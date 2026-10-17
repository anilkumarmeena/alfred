"""Tests for pictures: user photos reach Claude with the next turn; screen_look only on request (sips faked).

Linux paths (Pillow, the screenshot programs) run here too, with the OS, programs and environment faked.
"""

import asyncio
import base64
import io
import json
import os
import struct
import time
import zlib

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from conftest import use_profile
from jarvis import server, tools
from jarvis.brain import cli, sdk
from jarvis.core import platform
from jarvis.state import conversation, memory, pictures
from jarvis.storage import profile
from jarvis.tasks import worker
from jarvis.tools import screen


def png(width: int = 1, height: int = 1) -> bytes:
    """Real PNG header of the given size (pixels are only read by sips, which tests don't run)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


def jpeg(width: int, height: int) -> bytes:
    """JPEG start: APP0, then the start-of-frame giving its size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.fixture
def folders(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def sips(monkeypatch):
    """Fake sips/screencapture (on Linux, Pillow and the screenshot): records each call and writes a small image."""
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "sips" and "--out" in args:
            (pictures.Path(args[-1])).write_bytes(jpeg(800, 600))
        elif args[0] == "screencapture":
            (pictures.Path(args[-1])).write_bytes(png(2880, 1800))
        return pictures.subprocess.CompletedProcess(args, 0, "", "")

    def converted(src, out, quality, shrink):
        calls.append(["pillow", str(src), str(out), quality, shrink])
        out.write_bytes(jpeg(800, 600))

    def captured(raw):
        calls.append(["screenshot", str(raw)])
        raw.write_bytes(png(2880, 1800))

    monkeypatch.setattr(pictures, "_run", run)
    monkeypatch.setattr(pictures, "_pillow_jpeg", converted)
    monkeypatch.setattr(pictures, "_linux_screenshot", captured)
    return calls


mac_only = pytest.mark.skipif(not platform.MAC, reason="sips and screencapture are macOS's")


# ── What a picture is, and its size ────────────────────────────

def test_kind_comes_from_the_first_bytes():
    assert pictures.kind(png()) == "image/png"
    assert pictures.kind(jpeg(10, 10)) == "image/jpeg"
    assert pictures.kind(b"GIF89a" + b"\x00" * 10) == "image/gif"
    assert pictures.kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert pictures.kind(b"\x00\x00\x00\x18ftypheic\x00\x00") == "image/heic"
    assert pictures.kind(b"%PDF-1.7") is None
    assert pictures.kind(b"") is None


def test_dimensions_are_read_from_the_header():
    assert pictures.dimensions(png(640, 480), "image/png") == (640, 480)
    assert pictures.dimensions(jpeg(4032, 3024), "image/jpeg") == (4032, 3024)
    assert pictures.dimensions(b"GIF89a" + struct.pack("<HH", 300, 200), "image/gif") == (300, 200)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (1999).to_bytes(3, "little") + (999).to_bytes(3, "little")
    assert pictures.dimensions(vp8x, "image/webp") == (2000, 1000)
    assert pictures.dimensions(b"\xff\xd8garbage", "image/jpeg") is None
    assert pictures.dimensions(b"\x89PNG", "image/png") is None


# ── Made fit for Claude ────────────────────────────────────────

def test_a_picture_that_fits_is_kept_as_it_is(folders, sips):
    src = folders / "raw.part"
    src.write_bytes(png(1200, 800))
    out = pictures.fit(src, folders / "pic")
    assert out == folders / "pic.png" and out.read_bytes() == png(1200, 800)
    assert not src.exists() and sips == []


@mac_only
def test_a_big_picture_becomes_a_smaller_jpeg(folders, sips):
    src = folders / "raw.part"
    src.write_bytes(jpeg(4032, 3024))
    out = pictures.fit(src, folders / "pic")
    assert out == folders / "pic.jpg" and pictures.kind(out.read_bytes()) == "image/jpeg"
    assert not src.exists()
    assert sips == [["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(pictures.JPEG_QUALITY),
                     "-Z", str(pictures.MAX_EDGE), str(src), "--out", str(out)]]


@mac_only
def test_a_small_heic_becomes_a_jpeg_without_being_scaled_up(folders, monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        if "-g" in args:
            return pictures.subprocess.CompletedProcess(args, 0, "  pixelWidth: 800\n  pixelHeight: 600\n", "")
        pictures.Path(args[-1]).write_bytes(jpeg(800, 600))
        return pictures.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(pictures, "_run", run)
    src = folders / "raw.part"
    src.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100)
    assert pictures.fit(src, folders / "pic") == folders / "pic.jpg"
    assert "-Z" not in calls[-1] and calls[-1][:4] == ["sips", "-s", "format", "jpeg"]


@mac_only
def test_another_format_sips_reads_becomes_a_jpeg(folders, monkeypatch):
    def run(args):
        if "-g" in args:  # e.g. a TIFF: no header dimensions() reads, but sips knows its size
            return pictures.subprocess.CompletedProcess(args, 0, "  pixelWidth: 3000\n  pixelHeight: 2000\n", "")
        pictures.Path(args[-1]).write_bytes(jpeg(1568, 1045))
        return pictures.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(pictures, "_run", run)
    src = folders / "raw.part"
    src.write_bytes(b"II*\x00" + b"\x00" * 100)
    assert pictures.fit(src, folders / "pic") == folders / "pic.jpg"


def test_what_isnt_a_picture_is_refused(folders, sips):
    src = folders / "raw.part"
    src.write_bytes(b"%PDF-1.7 not a picture")
    with pytest.raises(ValueError, match="isn't a picture"):
        pictures.fit(src, folders / "pic")
    assert not src.exists()


# ── Photos the user sends ──────────────────────────────────────

def test_a_photo_waits_for_the_next_turn_which_takes_it_once(folders, sips):
    photo = pictures.save_photo(png(640, 480))
    assert pictures.NAME.fullmatch(photo)
    assert (pictures.inbox() / photo).is_file()
    taken = pictures.take()
    assert taken == [pictures.photos() / photo] and taken[0].read_bytes() == png(640, 480)
    assert pictures.take() == []  # one turn only


def test_several_photos_come_oldest_first_and_a_big_one_is_made_fit(folders, sips):
    first = pictures.save_photo(png(10, 10))
    time.sleep(0.01)
    second = pictures.save_photo(jpeg(4032, 3024))
    assert second.endswith(".jpg") and sips  # sips shrank it
    assert [p.name for p in pictures.take()] == sorted([first, second])


def test_a_photo_left_waiting_too_long_is_dropped(folders, sips):
    photo = pictures.save_photo(png())
    old = time.time() - pictures.INBOX_MINUTES * 60 - 5
    os.utime(pictures.inbox() / photo, (old, old))
    (pictures.inbox() / ".20260101-000000-abcdef.part").write_bytes(b"half")  # a save that died
    os.utime(pictures.inbox() / ".20260101-000000-abcdef.part", (old, old))
    assert pictures.take() == []
    assert list(pictures.inbox().iterdir()) == []


def test_a_photo_being_saved_isnt_taken(folders):
    pictures.inbox().mkdir(parents=True)
    (pictures.inbox() / ".20261008-120000-abcdef.part").write_bytes(png())
    assert pictures.take() == []


def test_too_big_or_not_a_picture_is_refused(folders, monkeypatch):
    with pytest.raises(ValueError, match="isn't a picture"):
        pictures.save_photo(b"hello")
    monkeypatch.setattr(pictures, "MAX_UPLOAD", 10)
    with pytest.raises(ValueError, match="too big"):
        pictures.save_photo(png())


def test_a_photo_can_be_taken_back_by_its_id_only(folders, sips):
    photo = pictures.save_photo(png())
    assert pictures.drop(photo) is True
    assert pictures.drop(photo) is False
    for bad in ("../tasks.db", "x.png", f"../inbox/{photo}", ""):
        with pytest.raises(ValueError):
            pictures.drop(bad)


def test_old_pictures_are_pruned(folders):
    folder = folders / "photos"
    folder.mkdir()
    now = time.time()
    for i in range(6):
        p = folder / f"p{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (now - i * 60, now - i * 60))  # p0 newest
    stale = folder / "stale.png"
    stale.write_bytes(b"x")
    os.utime(stale, (now - 10 * 86400, now - 10 * 86400))
    assert pictures.prune(folder, 86400, keep=4) == 3
    assert sorted(p.name for p in folder.iterdir()) == ["p0.png", "p1.png", "p2.png", "p3.png"]
    assert pictures.prune(folders / "missing", 1, 1) == 0


def test_block_is_a_base64_image_content_block(folders):
    p = folders / "a.png"
    p.write_bytes(png())
    assert pictures.block(p) == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                             "data": base64.b64encode(png()).decode()}}


# ── Screenshots ────────────────────────────────────────────────

@mac_only
def test_a_screenshot_is_silent_made_fit_and_kept_in_screens(folders, sips):
    shot = pictures.screenshot()
    assert shot.parent == pictures.screens() and shot.suffix == ".jpg" and shot.is_file()
    assert sips[0][:5] == ["screencapture", "-x", "-m", "-t", "png"]
    assert "-Z" in sips[1]  # a Retina screen exceeds MAX_EDGE
    assert [p for p in pictures.screens().iterdir()] == [shot]  # no half-made file left behind


@mac_only
def test_a_screenshot_macos_refuses_says_what_to_do(folders, monkeypatch):
    def refused(args):
        raise RuntimeError("screencapture failed: could not create image from display")

    monkeypatch.setattr(pictures, "_run", refused)
    with pytest.raises(RuntimeError, match="Screen Recording"):
        pictures.screenshot()
    assert list(pictures.screens().iterdir()) == []


def test_screenshots_are_pruned_sooner_than_photos(folders, sips):
    pictures.screens().mkdir(parents=True)
    old = pictures.screens() / "20260101-000000-abcdef.jpg"
    old.write_bytes(b"x")
    then = time.time() - pictures.SCREEN_HOURS * 3600 - 60
    os.utime(old, (then, then))
    pictures.screenshot()
    assert not old.exists()
    assert pictures.SCREEN_HOURS * 3600 < pictures.PHOTO_DAYS * 86400


# ── On Linux: Pillow and the desktop's screenshot programs ─────

@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(platform, "MAC", False)
    monkeypatch.setattr(platform, "LINUX", True)
    monkeypatch.setattr(platform, "WSL", False)


def real(fmt: str, size: tuple[int, int], mode: str = "RGB", color="red") -> bytes:
    """An actual picture made by Pillow."""
    from PIL import Image

    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt)
    return out.getvalue()


def opened(path):
    from PIL import Image

    with Image.open(path) as im:
        return im.format, im.size, im.convert("RGB").getpixel((0, 0))


def test_on_linux_a_big_picture_becomes_a_smaller_jpeg_by_pillow(folders, linux):
    src = folders / "raw.part"
    src.write_bytes(real("PNG", (3000, 2000)))
    out = pictures.fit(src, folders / "pic")
    assert out == folders / "pic.jpg" and not src.exists()
    assert opened(out)[:2] == ("JPEG", (pictures.MAX_EDGE, 1045))


def test_on_linux_a_picture_claude_cant_take_becomes_a_jpeg_without_being_scaled_up(folders, linux):
    src = folders / "raw.part"
    src.write_bytes(real("TIFF", (40, 30)))
    assert opened(pictures.fit(src, folders / "pic"))[:2] == ("JPEG", (40, 30))


def test_on_linux_transparency_turns_white_not_black(folders, linux):
    src = folders / "raw.part"
    src.write_bytes(real("PNG", (2000, 100), "RGBA", (0, 0, 0, 0)))
    assert opened(pictures.fit(src, folders / "pic"))[2] == (255, 255, 255)


def test_on_linux_a_picture_that_fits_is_kept_as_it_is(folders, linux):
    src = folders / "raw.part"
    src.write_bytes(data := real("PNG", (800, 600)))
    assert pictures.fit(src, folders / "pic").read_bytes() == data


def test_on_linux_a_heic_pillow_cant_open_says_how_to_read_it(folders, linux, monkeypatch):
    monkeypatch.setattr(pictures, "_pillow_size", lambda path: None)  # no pillow-heif
    src = folders / "raw.part"
    src.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100)
    with pytest.raises(ValueError, match="pillow-heif"):
        pictures.fit(src, folders / "pic")
    assert not src.exists()
    src.write_bytes(b"%PDF-1.7 not a picture")
    with pytest.raises(ValueError, match="isn't a picture"):
        pictures.fit(src, folders / "pic")


@pytest.fixture
def desktop(monkeypatch, linux):
    """A Wayland desktop with no screenshot program yet; `installed` adds some, `ran` records what ran."""
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    state = {"installed": set(), "ran": [], "fails": set()}
    monkeypatch.setattr(pictures.shutil, "which", lambda p: f"/usr/bin/{p}" if p in state["installed"] else None)

    def run(args):
        state["ran"].append(args[:-1])
        if args[0] in state["fails"]:
            raise RuntimeError(f"{args[0]} failed: compositor doesn't support it")
        pictures.Path(args[-1]).write_bytes(png(1920, 1080))
        return pictures.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(pictures, "_run", run)
    monkeypatch.setattr(pictures, "_pillow_jpeg", lambda src, out, quality, shrink: out.write_bytes(jpeg(800, 450)))
    return state


def test_on_wayland_the_first_screenshot_program_that_works_is_used(folders, desktop):
    desktop["installed"] |= {"grim", "gnome-screenshot", "scrot"}
    desktop["fails"].add("grim")  # GNOME: no wlr-screencopy
    shot = pictures.screenshot()
    assert desktop["ran"] == [["grim"], ["gnome-screenshot", "-f"]]
    assert shot.suffix == ".jpg" and list(pictures.screens().iterdir()) == [shot]


def test_on_x11_scrot_comes_first_and_on_wayland_it_isnt_used(folders, desktop, monkeypatch):
    desktop["installed"] |= {"scrot", "import"}
    with pytest.raises(RuntimeError, match="install grim or gnome-screenshot or spectacle"):
        pictures.screenshot()  # X11 programs see only a black screen on Wayland
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":0")
    pictures.screenshot()
    assert desktop["ran"] == [["scrot"]]


def test_on_linux_screenshots_that_cant_be_taken_say_why(folders, desktop, monkeypatch):
    desktop["installed"].add("grim")
    desktop["fails"].add("grim")
    with pytest.raises(RuntimeError, match="Couldn't take a screenshot: grim failed"):
        pictures.screenshot()
    monkeypatch.delenv("WAYLAND_DISPLAY")
    with pytest.raises(RuntimeError, match="no desktop session"):
        pictures.screenshot()
    monkeypatch.setattr(platform, "WSL", True)
    with pytest.raises(RuntimeError, match="can't see the Windows screen"):
        pictures.screenshot()
    assert list(pictures.screens().iterdir()) == []  # nothing half-made left


def test_on_linux_screen_look_says_what_it_took_without_macos_advice(folders, sips, linux):
    content, is_error = run_tool("what's on my screen?")
    text, image = content
    assert not is_error and image["type"] == "image"
    assert "this computer's screen" in text["text"] and "macOS" not in text["text"]


def test_on_linux_screen_look_reports_a_missing_program_cleanly(folders, desktop):
    content, is_error = run_tool("look at my screen")
    assert is_error and "install grim" in content


# ── screen_look: only when the user asks ───────────────────────

@pytest.mark.parametrize("said", ["Look at my screen.", "What's this error on my screen?", "can you see my display",
                                  "take a screenshot and tell me what's wrong", "what's on my monitor"])
def test_asking_about_the_screen_lets_it_look(said):
    assert screen.asked(said, "")


@pytest.mark.parametrize("said, before", [
    ("What's the weather?", ""),
    ("yes", "Shall I send it?"),  # a yes to something else
    ("look at this web page", ""),
    ("no", "Want me to look at your screen?"),
    ("yes", "I looked at your screen."),  # not a question
])
def test_anything_else_doesnt(said, before):
    assert not screen.asked(said, before)


def test_a_yes_to_jarvis_asking_lets_it_look():
    assert screen.asked("Yes please.", "Want me to look at your screen?")
    assert screen.asked("yeah go ahead", "I could take a look at your screen, shall I?")


def run_tool(said: str, before: str = ""):
    conversation.current().heard.replied(before)
    conversation.current().heard.turn(said)
    return asyncio.run(tools.run("screen_look", {}))


def test_screen_look_is_refused_unless_the_user_asked(folders, sips):
    content, is_error = run_tool("summarise this web page for me")
    assert is_error and "Not taken" in content and "ask them" in content
    assert sips == []  # no screenshot taken


@mac_only
def test_screen_look_shows_claude_the_screenshot(folders, sips):
    content, is_error = run_tool("what's on my screen?")
    text, image = content
    assert not is_error and "Screen Recording" in text["text"] and image["type"] == "image"
    assert [p.suffix for p in pictures.screens().iterdir()] == [".jpg"]  # kept in screens


def test_the_sdk_tool_sends_the_screenshot_as_an_image_block(folders, sips):
    conversation.current().heard.turn("look at my screen")
    handler = sdk._sdk_tool(tools.get("screen_look")).handler
    result = asyncio.run(handler({}))
    text, image = result["content"]
    assert text["type"] == "text" and "screenshot" in text["text"]
    assert image["type"] == "image" and image["mimeType"] == "image/jpeg"
    assert base64.b64decode(image["data"]) == jpeg(800, 600)
    assert result["is_error"] is False


def test_the_voice_brain_gets_screen_look_and_the_guidance_but_tasks_dont():
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    assert "mcp__jarvis__screen_look" in opts.allowed_tools
    assert "screen_look" in opts.system_prompt and "Pictures:" in opts.system_prompt
    assert "screen_look" not in worker.READ_ONLY_TOOLS
    assert not tools.get("screen_look").gated and not tools.get("screen_look").changes


# ── A photo reaches Claude with the next turn ──────────────────

class PhotoClient:
    """Fake ClaudeSDKClient that keeps what each query sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def query(self, prompt) -> None:
        if isinstance(prompt, str):
            self.sent.append(prompt)
        else:
            self.sent.append([m async for m in prompt])

    async def receive_response(self):
        yield StreamEvent(uuid="1", session_id="s", event={"type": "content_block_delta",
                                                           "delta": {"type": "text_delta", "text": "A cat."}})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s")


def test_a_photo_sent_goes_to_claude_as_an_image_with_the_users_words(folders, sips, monkeypatch):
    logged = []
    monkeypatch.setattr(memory, "log_exchange", lambda user, assistant, approved=(), interrupted=False:
                        logged.append((user, assistant)))
    brain = cli.CliBrain()
    brain.client = client = PhotoClient()
    pictures.save_photo(png(64, 64))

    async def talk(text):
        return "".join([chunk async for chunk in brain.respond(text)])

    assert asyncio.run(talk("what's this?")) == "A cat."
    [message] = client.sent[0]
    image, text = message["message"]["content"]
    assert message["type"] == "user" and message["message"]["role"] == "user"
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png(64, 64)).decode()}}
    assert text["type"] == "text"
    assert text["text"].endswith(f"what's this?\n[{profile.name()} sent a photo with this, attached.]")
    assert logged[-1] == ("what's this? [sent a photo]", "A cat.")

    asyncio.run(talk("and now?"))  # the photo went with the previous turn
    assert isinstance(client.sent[1], str) and "photo" not in client.sent[1]
    assert logged[-1] == ("and now?", "A cat.")
    assert conversation.current().heard.before == "A cat."  # what the second turn answered


# ── The web server takes the photo ─────────────────────────────

class Request:
    remote = "127.0.0.1"
    host = "localhost:8787"
    headers: dict = {}

    def __init__(self, body) -> None:
        self.body = body

    async def json(self):
        return self.body


def post(handler, body: dict):
    r = asyncio.run(handler(Request(body)))
    return r.status, json.loads(r.text)


def test_the_server_keeps_a_photo_for_the_next_turn(folders, sips, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    server._failures.clear()
    status, body = post(server.picture, {"passcode": "123456", "data": base64.b64encode(png(32, 32)).decode()})
    assert status == 200 and pictures.NAME.fullmatch(body["id"])
    assert (pictures.inbox() / body["id"]).is_file()
    assert post(server.picture_drop, {"passcode": "123456", "id": body["id"]}) == (200, {"ok": True, "dropped": True})
    assert pictures.take() == []


def test_the_server_turns_away_a_wrong_passcode_or_what_isnt_a_photo(folders, sips, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    server._failures.clear()
    data = base64.b64encode(png()).decode()
    assert post(server.picture, {"passcode": "wrong", "data": data})[0] == 401
    assert post(server.picture, {"passcode": "123456", "data": "not base64!"})[0] == 400
    status, body = post(server.picture, {"passcode": "123456", "data": base64.b64encode(b"%PDF-1.7").decode()})
    assert status == 400 and "isn't a picture" in body["error"]
    assert post(server.picture_drop, {"passcode": "123456", "id": "../tasks.db"})[0] == 400
    server._failures.clear()
    assert not pictures.inbox().exists() or list(pictures.inbox().iterdir()) == []


def test_the_server_takes_a_request_as_big_as_the_biggest_photo():
    assert server.app()._client_max_size >= pictures.MAX_UPLOAD * 4 // 3
