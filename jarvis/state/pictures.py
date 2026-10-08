"""Pictures JARVIS looks at: photos the user sends from the phone or a browser, and screenshots of the Mac's screen.

A photo comes in through jarvis.server (POST /api/picture) and waits in the profile's pictures/inbox until the voice brain's
next turn, spoken or typed, takes it (`take`) and shows it to Claude with the user's words, as an image content block.
One left waiting more than INBOX_MINUTES is dropped instead, so it can't turn up in an unrelated conversation later.
Screenshots are taken only by the screen_look tool (jarvis.tools.screen), when the user asks.

Each is made to fit what Claude reads well (`fit`): a JPEG, PNG, GIF or WebP at most MAX_EDGE pixels on its long side
and MAX_BYTES in size. Anything bigger, or in another format (an iPhone's HEIC, a TIFF), becomes a JPEG through macOS's
own sips; pictures in documents and attachments are made to fit the same way (jarvis.core.documents). They're kept in
the profile's pictures folder, photos for PHOTO_DAYS and screenshots for SCREEN_HOURS (at most the newest PHOTOS_KEPT and
SCREENS_KEPT), pruned whenever one is added.
"""

import base64
import logging
import re
import secrets
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jarvis.core import profile

log = logging.getLogger("jarvis.pictures")


def inbox() -> Path:
    return profile.current().pictures / "inbox"  # sent, waiting for the next turn


def photos() -> Path:
    return profile.current().pictures / "photos"  # sent and shown to Claude


def screens() -> Path:
    return profile.current().pictures / "screens"  # screenshots


MAX_EDGE = 1568  # pixels on the long side: more costs tokens without Claude seeing more
MAX_BYTES = 3_750_000  # Claude takes up to 5 MB of base64 per image
MAX_UPLOAD = 15_000_000  # the most a sent photo may be before it's made to fit
JPEG_QUALITY = 82
INBOX_MINUTES = 10
PHOTO_DAYS, PHOTOS_KEPT = 7, 50
SCREEN_HOURS, SCREENS_KEPT = 24, 20
TIMEOUT = 30  # seconds for screencapture or sips

SENDABLE = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MEDIA = {ext: media for media, ext in SENDABLE.items()}
NAME = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}\.(jpg|png|gif|webp)")  # a picture's file name, which is also its id

NOT_A_PICTURE = "That isn't a picture JARVIS can read: send a JPEG, PNG, GIF, WebP or HEIC."
SCREEN_PERMISSION = ("macOS may need to let JARVIS record the screen: in System Settings, Privacy & Security, Screen "
                     "Recording, turn on JARVIS's voice agent, then restart JARVIS.")


def kind(data: bytes) -> str | None:
    """The picture's media type, from its first bytes: one Claude takes (SENDABLE), "image/heic", or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif"):
        return "image/heic"
    return None


def dimensions(data: bytes, media: str | None) -> tuple[int, int] | None:
    """Width and height, read from the header of a JPEG, PNG, GIF or WebP; None if it can't tell."""
    try:
        if media == "image/png":
            return struct.unpack(">II", data[16:24])
        if media == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if media == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            return None
        if media == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker == 0xFF:  # padding
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length follows
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start of frame
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
    except struct.error:
        pass
    return None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """One of macOS's own programs (sips, screencapture), with its arguments as argv; RuntimeError if it fails."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{args[0]} didn't run: {e}") from None
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(done.stderr or done.stdout).strip()[:300] or done.returncode}")
    return done


def _sips_size(path: Path) -> tuple[int, int] | None:
    """Width and height as sips reads them, for a format whose header dimensions() doesn't (HEIC, TIFF); None if it
    can't, as when it isn't a picture at all."""
    try:
        out = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)]).stdout
    except RuntimeError:
        return None
    found = dict(re.findall(r"(pixelWidth|pixelHeight):\s*(\d+)", out))
    return (int(found["pixelWidth"]), int(found["pixelHeight"])) if len(found) == 2 else None


def fit(src: Path, stem: Path) -> Path:
    """`src` made fit for Claude and moved to `stem` plus its extension, which is returned. Kept as it is if it already
    fits; else a JPEG, scaled down to MAX_EDGE if it's bigger. ValueError if it isn't a picture sips can read;
    RuntimeError if sips fails."""
    data = src.read_bytes()
    media = kind(data)
    size = dimensions(data, media) or _sips_size(src)
    if media is None and size is None:
        src.unlink(missing_ok=True)
        raise ValueError(NOT_A_PICTURE)
    if media in SENDABLE and size and max(size) <= MAX_EDGE and len(data) <= MAX_BYTES:
        return src.rename(stem.with_suffix(SENDABLE[media]))
    out = stem.with_suffix(".jpg")
    try:
        for quality in (JPEG_QUALITY, 60):
            args = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
            if not size or max(size) > MAX_EDGE:  # only ever down: sips -Z scales small pictures up too
                args += ["-Z", str(MAX_EDGE)]
            _run([*args, str(src), "--out", str(out)])
            if out.stat().st_size <= MAX_BYTES:
                return out
        raise ValueError("That picture is too big to send to Claude, even made smaller.")
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        src.unlink(missing_ok=True)


def _stamp() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def prune(folder: Path, max_age: float, keep: int) -> int:
    """Delete the pictures in `folder` older than `max_age` seconds, and all but the newest `keep`; how many went."""
    if not folder.is_dir():
        return 0
    found = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    now, gone = time.time(), 0
    for i, p in enumerate(found):
        if i >= keep or now - p.stat().st_mtime > max_age:
            p.unlink(missing_ok=True)
            gone += 1
    return gone


def prune_all() -> None:
    prune(photos(), PHOTO_DAYS * 86400, PHOTOS_KEPT)
    prune(screens(), SCREEN_HOURS * 3600, SCREENS_KEPT)


def _expire() -> None:
    """Drop what has waited in the inbox more than INBOX_MINUTES: photos nobody asked about, and saves that died
    half way."""
    if not inbox().is_dir():
        return
    for p in inbox().iterdir():
        try:
            if time.time() - p.stat().st_mtime > INBOX_MINUTES * 60:
                log.info("%s waited too long; dropped", p.name)
                p.unlink(missing_ok=True)
        except FileNotFoundError:  # taken by a turn meanwhile
            pass


def save_photo(data: bytes) -> str:
    """Keep a photo the user sent for the next turn; returns its id. ValueError if it isn't a picture or is too big."""
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That photo is too big: the most is {MAX_UPLOAD // 1_000_000} MB.")
    if kind(data) is None:
        raise ValueError(NOT_A_PICTURE)
    inbox().mkdir(parents=True, exist_ok=True)
    _expire()
    stamp = _stamp()
    raw = inbox() / f".{stamp}.part"  # hidden while it's made to fit, so a turn can't take it half made
    raw.write_bytes(data)
    fitted = fit(raw, inbox() / f".{stamp}")
    final = inbox() / fitted.name.lstrip(".")
    fitted.rename(final)
    log.info("photo %s waiting for the next turn (%d bytes)", final.name, final.stat().st_size)
    return final.name


def drop(photo_id: str) -> bool:
    """Take back a photo still waiting for its turn, as the page does when the user removes it."""
    if not NAME.fullmatch(photo_id):
        raise ValueError("That isn't a photo's id.")
    path = inbox() / photo_id
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def take() -> list[Path]:
    """The photos sent since the last turn, oldest first, moved out of the inbox to be shown to Claude now. Ones
    that waited more than INBOX_MINUTES are dropped instead."""
    if not inbox().is_dir():
        return []
    _expire()
    waiting = sorted(p for p in inbox().iterdir() if NAME.fullmatch(p.name))
    if not waiting:
        return []
    photos().mkdir(parents=True, exist_ok=True)
    taken = [p.rename(photos() / p.name) for p in waiting]
    prune_all()
    return taken


def screenshot() -> Path:
    """A screenshot of the Mac's main display, made fit for Claude, kept in data/pictures/screens. Silent (no shutter
    sound). RuntimeError, saying what to do, if macOS won't take it."""
    screens().mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    raw = screens() / f".{stamp}.part"
    try:
        _run(["screencapture", "-x", "-m", "-t", "png", str(raw)])
    except RuntimeError as e:
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"Couldn't take a screenshot ({e}). {SCREEN_PERMISSION}") from None
    if not raw.is_file() or raw.stat().st_size == 0:
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"Couldn't take a screenshot. {SCREEN_PERMISSION}")
    shot = fit(raw, screens() / stamp)
    prune_all()
    return shot


def read(path: Path) -> tuple[str, bytes]:
    """A picture `fit` made: its media type and bytes."""
    return MEDIA[path.suffix], path.read_bytes()


def block(path: Path) -> dict:
    """The picture as a content block for a message to Claude."""
    media, data = read(path)
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(data).decode()}}
