"""Pictures for the model: photos sent from the phone/browser, and on-demand screenshots.

Sent photos wait in pictures/inbox until the next voice turn take()s them; after INBOX_MINUTES they are dropped so
they can't surface in an unrelated conversation. fit() converts anything over MAX_EDGE / MAX_BYTES or not
JPEG/PNG/GIF/WebP (e.g. HEIC, TIFF) to JPEG via sips on macOS, Pillow on Linux (HEIC needs pillow-heif). Photos are
kept PHOTO_DAYS, screenshots SCREEN_HOURS (capped by count), pruned on each add.
Screenshots: screencapture on macOS; on Linux grim, gnome-screenshot, spectacle, scrot or ImageMagick's import.
"""

import base64
import logging
import os
import re
import secrets
import shutil
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jarvis.core import platform
from jarvis.storage import profile

log = logging.getLogger("jarvis.pictures")


def inbox() -> Path:
    return profile.current().pictures / "inbox"  # awaiting the next turn


def photos() -> Path:
    return profile.current().pictures / "photos"  # already shown to Claude


def screens() -> Path:
    return profile.current().pictures / "screens"  # screenshots


MAX_EDGE = 1568  # long-side px; larger costs tokens for no gain
MAX_BYTES = 3_750_000  # stays under Claude's 5 MB base64 limit
MAX_UPLOAD = 15_000_000  # max upload before fitting
JPEG_QUALITY = 82
INBOX_MINUTES = 10
PHOTO_DAYS, PHOTOS_KEPT = 7, 50
SCREEN_HOURS, SCREENS_KEPT = 24, 20
TIMEOUT = 30  # seconds for a screenshot program or sips

SENDABLE = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MEDIA = {ext: media for media, ext in SENDABLE.items()}
NAME = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}\.(jpg|png|gif|webp)")  # file name, also the picture id

NOT_A_PICTURE = "That isn't a picture JARVIS can read: send a JPEG, PNG, GIF, WebP or HEIC."
SCREEN_PERMISSION = ("macOS may need to let JARVIS record the screen: in System Settings, Privacy & Security, Screen "
                     "Recording, turn on JARVIS's voice agent, then restart JARVIS.") if platform.MAC else ""
HEIC_UNREADABLE = ("That's a HEIC photo, which JARVIS can't read on this computer yet: install pillow-heif (`uv pip "
                   "install pillow-heif` in JARVIS's folder), or send it as a JPEG.")
# Linux screenshot programs, by session type, best first: (program, argv before the file name, package)
WAYLAND_SHOTS = [("grim", ["grim"], "grim"),  # wlroots desktops: Sway, Hyprland
                 ("gnome-screenshot", ["gnome-screenshot", "-f"], "gnome-screenshot"),
                 ("spectacle", ["spectacle", "-b", "-n", "-f", "-o"], "spectacle")]  # KDE
X11_SHOTS = [("scrot", ["scrot"], "scrot"),
             ("import", ["import", "-window", "root"], "imagemagick"),
             ("gnome-screenshot", ["gnome-screenshot", "-f"], "gnome-screenshot"),
             ("spectacle", ["spectacle", "-b", "-n", "-f", "-o"], "spectacle")]


def kind(data: bytes) -> str | None:
    """Media type sniffed from magic bytes: a SENDABLE type, "image/heic", or None."""
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
    """(width, height) from a JPEG/PNG/GIF/WebP header; None if unknown."""
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
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
    except struct.error:
        pass
    return None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run sips or a screenshot program; RuntimeError on failure."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{args[0]} didn't run: {e}") from None
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(done.stderr or done.stdout).strip()[:300] or done.returncode}")
    return done


def _sips_size(path: Path) -> tuple[int, int] | None:
    """(width, height) via sips for formats dimensions() can't read; None if not a picture."""
    try:
        out = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)]).stdout
    except RuntimeError:
        return None
    found = dict(re.findall(r"(pixelWidth|pixelHeight):\s*(\d+)", out))
    return (int(found["pixelWidth"]), int(found["pixelHeight"])) if len(found) == 2 else None


def _pillow():
    """PIL.Image, with HEIC opening if pillow-heif is installed."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("The picture reader (Pillow) isn't installed: run `uv sync` in JARVIS's folder.") from None
    try:
        from pillow_heif import register_heif_opener  # pyright: ignore[reportMissingImports]  # Linux only
        register_heif_opener()
    except ImportError:
        pass
    return Image


def _pillow_size(path: Path) -> tuple[int, int] | None:
    """(width, height) via Pillow for formats dimensions() can't read; None if not a picture it reads."""
    Image = _pillow()
    try:
        with Image.open(path) as im:
            return im.size
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _pillow_jpeg(src: Path, out: Path, quality: int, shrink: bool) -> None:
    """Write `src` as a JPEG to `out`, upright, transparency on white, its long side cut to MAX_EDGE if `shrink`."""
    Image = _pillow()
    from PIL import ImageOps

    try:
        with Image.open(src) as im:
            if shrink:
                im.draft("RGB", (MAX_EDGE, MAX_EDGE))  # JPEGs decode smaller, faster
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                im = Image.new("RGB", rgba.size, "white")
                im.paste(rgba, mask=rgba.getchannel("A"))
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if shrink:
                im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            im.save(out, "JPEG", quality=quality, optimize=True)
    except Image.DecompressionBombError:
        raise ValueError("That picture has far too many pixels to read.") from None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Pillow couldn't convert it: {e}") from None


def _size(path: Path) -> tuple[int, int] | None:
    return _sips_size(path) if platform.MAC else _pillow_size(path)


def _jpeg(src: Path, out: Path, quality: int, shrink: bool) -> None:
    if not platform.MAC:
        return _pillow_jpeg(src, out, quality, shrink)
    args = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    if shrink:  # sips -Z would also upscale
        args += ["-Z", str(MAX_EDGE)]
    _run([*args, str(src), "--out", str(out)])


def fit(src: Path, stem: Path) -> Path:
    """Move `src` to `stem`+ext, converting to a (downscaled) JPEG if needed; return the new path. Consumes `src`.

    ValueError if not a readable picture or still too big; RuntimeError if sips or Pillow fails."""
    data = src.read_bytes()
    media = kind(data)
    size = dimensions(data, media) or _size(src)
    if size is None and (media is None or (media == "image/heic" and not platform.MAC)):
        src.unlink(missing_ok=True)
        raise ValueError(HEIC_UNREADABLE if media else NOT_A_PICTURE)
    if media in SENDABLE and size and max(size) <= MAX_EDGE and len(data) <= MAX_BYTES:
        return src.rename(stem.with_suffix(SENDABLE[media]))
    out = stem.with_suffix(".jpg")
    try:
        for quality in (JPEG_QUALITY, 60):
            _jpeg(src, out, quality, shrink=not size or max(size) > MAX_EDGE)
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
    """Delete files older than `max_age` seconds or beyond the newest `keep`; return the count."""
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
    """Delete inbox files older than INBOX_MINUTES (unclaimed photos, abandoned partial saves)."""
    if not inbox().is_dir():
        return
    for p in inbox().iterdir():
        try:
            if time.time() - p.stat().st_mtime > INBOX_MINUTES * 60:
                log.info("%s waited too long; dropped", p.name)
                p.unlink(missing_ok=True)
        except FileNotFoundError:  # taken concurrently
            pass


def save_photo(data: bytes) -> str:
    """Queue an uploaded photo for the next turn; return its id. ValueError if not a picture or too big."""
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"That photo is too big: the most is {MAX_UPLOAD // 1_000_000} MB.")
    if kind(data) is None:
        raise ValueError(NOT_A_PICTURE)
    inbox().mkdir(parents=True, exist_ok=True)
    _expire()
    stamp = _stamp()
    raw = inbox() / f".{stamp}.part"  # dot-prefixed so take() ignores it until done
    raw.write_bytes(data)
    fitted = fit(raw, inbox() / f".{stamp}")
    final = inbox() / fitted.name.lstrip(".")
    fitted.rename(final)
    log.info("photo %s waiting for the next turn (%d bytes)", final.name, final.stat().st_size)
    return final.name


def drop(photo_id: str) -> bool:
    """Remove a queued photo (user removed it in the page)."""
    if not NAME.fullmatch(photo_id):
        raise ValueError("That isn't a photo's id.")
    path = inbox() / photo_id
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def take() -> list[Path]:
    """Move queued photos (oldest first) out of the inbox for this turn; expired ones are dropped."""
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


def _shooters() -> list[tuple[str, list[str], str]]:
    """Linux screenshot programs for this session, best first; installed or not."""
    return WAYLAND_SHOTS if os.environ.get("WAYLAND_DISPLAY") else X11_SHOTS


def _no_screen() -> str:
    """Why screenshots can't be taken on this Linux machine at all; "" if they might."""
    if platform.WSL:
        return "JARVIS runs in WSL here, which can't see the Windows screen, so it can't take screenshots."
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return ("There's no screen JARVIS can reach here (no desktop session: DISPLAY or WAYLAND_DISPLAY isn't set), "
                "so it can't take screenshots.")
    if not any(shutil.which(program) for program, _, _ in _shooters()):
        packages = " or ".join(dict.fromkeys(package for _, _, package in _shooters()))
        return f"No screenshot program is installed, so it can't take screenshots: install {packages}."
    return ""


def _linux_screenshot(raw: Path) -> None:
    """Capture the screen to `raw` with the first program that works; RuntimeError saying why if none does."""
    if why := _no_screen():
        raise RuntimeError(why)
    problems = []
    for program, argv, _ in _shooters():
        if not shutil.which(program):
            continue
        try:
            _run([*argv, str(raw)])
        except RuntimeError as e:
            problems.append(str(e))
            raw.unlink(missing_ok=True)
            continue
        if raw.is_file() and raw.stat().st_size:
            return
        problems.append(f"{program} made no picture")
    raise RuntimeError(f"Couldn't take a screenshot: {'; '.join(problems)}.")


def screenshot() -> Path:
    """Silent screenshot of the main display, fitted for Claude. RuntimeError with a permissions hint on failure."""
    screens().mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    if not platform.MAC:
        raw = screens() / f".{stamp}.part.png"  # programs pick the format from the extension
        try:
            _linux_screenshot(raw)
        except RuntimeError:
            raw.unlink(missing_ok=True)
            raise
        shot = fit(raw, screens() / stamp)
        prune_all()
        return shot
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
    """(media type, bytes) of a fitted picture."""
    return MEDIA[path.suffix], path.read_bytes()


def block(path: Path) -> dict:
    """Base64 image content block for a Claude message."""
    media, data = read(path)
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(data).decode()}}
