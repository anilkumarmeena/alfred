"""The Linux desktop: user folders, notifications, the clipboard, opening links and files, the volume.

Each action runs a common desktop program with argv, never through a shell, and only if it's installed: notify-send
(libnotify-bin), xdg-open (xdg-utils), wl-copy/wl-paste (wl-clipboard) or xclip/xsel, wpctl or pactl. On macOS
JARVIS uses AI Armory's mac tools instead; only `user_dir` is used there, and gives the usual ~/ folders.
Imports nothing from JARVIS but `platform`: config uses `user_dir`.
"""

import asyncio
import os
import shlex
import shutil
from pathlib import Path

from jarvis.core import platform

TIMEOUT = 15  # seconds for a desktop program
OPEN_WAIT = 5  # seconds xdg-open gets to fail; past that it's showing the thing (some wait for the app to close)
CLIPBOARD_LIMIT = 20_000  # as AI Armory's mac_read_clipboard
NOT_HERE = "no desktop session (DISPLAY or WAYLAND_DISPLAY isn't set)"


# ── User folders ────────────────────────────────────────────────

def _user_dirs_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"


def _xdg_dir(key: str) -> str:
    """XDG_<key>_DIR from user-dirs.dirs, else the environment, as xdg-user-dir reads it; "" if unset."""
    try:
        lines = _user_dirs_file().read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        name, _, value = line.strip().partition("=")
        if name == f"XDG_{key}_DIR":
            try:
                value = "".join(shlex.split(value))
            except ValueError:
                return ""
            return value.replace("$HOME", str(Path.home()), 1)
    return os.environ.get(f"XDG_{key}_DIR", "")


def user_dir(key: str, english: str) -> str:
    """'~/Downloads'-style path of a user folder (key DOWNLOAD, DESKTOP, DOCUMENTS).

    On Linux it's the XDG user folder, which may be translated (~/Téléchargements); English name if unset."""
    if platform.LINUX and (found := _xdg_dir(key)):
        path = Path(found).expanduser()
        if path.is_absolute() and path != Path.home():  # set to $HOME means the folder is turned off
            return f"~/{path.relative_to(Path.home())}" if path.is_relative_to(Path.home()) else str(path)
    return f"~/{english}"


# The user's own file folders: where a bare file name is looked for, and where skills may be drafted from
USER_FOLDERS = [user_dir("DOWNLOAD", "Downloads"), user_dir("DESKTOP", "Desktop"), user_dir("DOCUMENTS", "Documents")]


# ── Running programs ────────────────────────────────────────────

def graphical() -> bool:
    """A desktop session JARVIS can reach (X11 or Wayland)."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


async def run(*argv: str, input: bytes | None = None, timeout: float = TIMEOUT, output: bool = True) -> str:
    """The program's output; RuntimeError with its stderr if it fails. Killed at the timeout.

    `output=False` for clipboard owners (wl-copy, xclip, xsel) that stay behind holding the copy: with pipes they'd
    keep them open and the call would never end."""
    pipe = asyncio.subprocess.PIPE if output else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=pipe, stderr=pipe)
    except OSError as e:
        raise RuntimeError(f"{argv[0]} didn't run: {e}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{argv[0]} didn't finish in {timeout:g} seconds.") from None
    if proc.returncode:
        said = (err or out or b"").decode(errors="replace").strip()[:300]
        raise RuntimeError(said or f"{argv[0]} exited with {proc.returncode}")
    return (out or b"").decode(errors="replace").strip()


# ── Notifications ───────────────────────────────────────────────

def can_notify() -> bool:
    return platform.LINUX and graphical() and bool(shutil.which("notify-send"))


async def notify(title: str, message: str) -> str:
    if not can_notify():
        raise RuntimeError(f"Can't show notifications here: {_missing('notify-send', 'libnotify-bin')}.")
    await run("notify-send", "--app-name=JARVIS", "--", title, message)
    return "Notification shown."


def _missing(program: str, package: str) -> str:
    return NOT_HERE if not graphical() else f"{program} isn't installed (package {package})"


# ── Opening links and files ─────────────────────────────────────

def can_open() -> bool:
    return platform.LINUX and graphical() and bool(shutil.which("xdg-open"))


async def open_target(target: str) -> str:
    """Open an http(s) link, or an existing file or folder, in its usual app. Programs and launchers aren't opened."""
    target = target.strip()
    if not target.startswith(("http://", "https://")):
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        if not path.exists():
            raise ValueError(f"Nothing to open at {path}: give a link starting with http(s)://, or a file or folder.")
        if path.suffix == ".desktop" or (path.is_file() and os.access(path, os.X_OK)):
            raise ValueError(f"{path.name} is a program or a launcher: JARVIS opens links, files and folders only.")
        target = str(path)
    if not can_open():
        raise RuntimeError(f"Can't open things here: {_missing('xdg-open', 'xdg-utils')}.")
    try:
        proc = await asyncio.create_subprocess_exec("xdg-open", target, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except OSError as e:
        raise RuntimeError(f"xdg-open didn't run: {e}") from None
    try:
        _, err = await asyncio.wait_for(proc.communicate(), OPEN_WAIT)
    except TimeoutError:
        return f"Opened {target}."
    if proc.returncode:
        raise RuntimeError((err or b"").decode(errors="replace").strip()[:300]
                           or f"xdg-open couldn't open {target} (exit {proc.returncode}).")
    return f"Opened {target}."


# ── The clipboard ───────────────────────────────────────────────

def _clipboard() -> tuple[list[str], list[str]] | None:
    """(copy argv, paste argv) for this session's clipboard program; None if there's none."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--no-newline", "--type", "text"]
    if os.environ.get("DISPLAY"):  # X11, or XWayland
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"]
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]
    return None


def can_copy() -> bool:
    return platform.LINUX and _clipboard() is not None


def _no_clipboard() -> str:
    package = "wl-clipboard" if os.environ.get("WAYLAND_DISPLAY") else "xclip or xsel"
    return f"No clipboard here: {NOT_HERE if not graphical() else f'install {package}'}."


async def copy(text: str) -> str:
    if not text:
        raise ValueError("Nothing copied: the text is empty.")
    if not (found := _clipboard()):
        raise RuntimeError(_no_clipboard())
    await run(*found[0], input=text.encode(), output=False)
    return f"Copied {len(text)} characters to the clipboard."


async def read_clipboard() -> str:
    if not (found := _clipboard()):
        raise RuntimeError(_no_clipboard())
    try:
        text = await run(*found[1])
    except RuntimeError as e:  # empty, or holding only a picture or files
        if any(w in str(e).lower() for w in ("nothing is copied", "no suitable type", "not available", "no selection")):
            return "The clipboard holds no text."
        raise
    if not text:
        return "The clipboard holds no text."
    if len(text) > CLIPBOARD_LIMIT:
        return f"{text[:CLIPBOARD_LIMIT]}\n… {len(text) - CLIPBOARD_LIMIT} more characters not shown."
    return text


# ── The volume ──────────────────────────────────────────────────

def _mixer() -> str | None:
    return next((p for p in ("wpctl", "pactl") if shutil.which(p)), None)


def can_set_volume() -> bool:
    return platform.LINUX and _mixer() is not None


async def set_volume(level: int) -> str:
    level = max(0, min(100, level))
    match _mixer():
        case "wpctl":  # PipeWire
            await run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}")
        case "pactl":  # PulseAudio, or PipeWire's pulse server
            await run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
        case _:
            raise RuntimeError("Can't set the volume here: install wireplumber (wpctl) or pulseaudio-utils (pactl).")
    return f"Volume set to {level}."
