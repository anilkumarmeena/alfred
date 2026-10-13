"""audio_file: render text or a local document to an audio file in JARVIS's voice (Kokoro).

Runs as a command job in the task runner, never in the conversation: visible in the task list, tracked by
job_status, stopped by stop_job/task_stop, announced on completion from its last line. Optionally sent to the phone.
"""

import asyncio
import json
import re
import secrets
import shlex
import sys
import time

from jarvis.core import config, platform
from jarvis.core.files import write_atomic
from jarvis.tasks import client
from jarvis.voice import audio_file as render

from . import tool
from .files import read_file

RENDER_SPEED = 1.0  # audio seconds rendered per second (0.9-1.8 on an 8 GB M2)


def _document(path: str) -> tuple[str, str]:
    """(full text, file stem) of a document."""
    p, doc = read_file(path)
    if not doc.text.strip():
        raise ValueError(f"{p.name} has no text to read" + (" (it's a scan or pictures)." if doc.pages else "."))
    return doc.text, p.stem


def _first_words(text: str, n: int = 6) -> str:
    return " ".join(re.sub(r"[^\w\s'-]", " ", text).split()[:n])


@tool(
    "audio_file",
    "Read text into an audio file in JARVIS's voice, for the user to listen to later: a summary or briefing you write "
    "(put the whole of it in text, written to be heard), something they dictate, or a document on the computer (path). "
    "For an email, a Google Doc or a web page, read it first, then pass its text, or your summary of it. It's made "
    "in the background, in parts, and saved as m4a (or mp3 or wav) in the user's Downloads unless JARVIS is set up "
    "otherwise; this says how long it will be and roughly how long it takes. Tell the user in a sentence, don't "
    "read the text aloud, and don't wait for it: JARVIS says when it's done and where it went. job_status shows how "
    "far it's got; stop_job cancels it. With phone, the phone also gets a notification that plays it.",
    {
        "text": {"type": "string", "description": "The words to read, in full. Markdown is fine: it's read as plain "
                                                  "speech. Give this or path."},
        "path": {"type": "string", "description": "Or a document on the computer to read, by its path or just its name "
                                                  "(PDF, Word, text...), looked for as file_read does."},
        "title": {"type": "string", "description": "A short name for the file, e.g. 'Monday briefing'. Default: the "
                                                   "document's name, or the text's first words."},
        "format": {"type": "string", "enum": list(config.AUDIO_FORMATS),
                   "description": f"Default {config.AUDIO_FORMAT}. mp3 needs ffmpeg or lame on the computer."},
        "voice": {"type": "string", "description": "A Kokoro voice, such as bf_emma or am_adam, only if the user asks "
                                                   "for another voice. Default: JARVIS's own."},
        "phone": {"type": "boolean", "description": "Also send it to the user's phone. Only when they ask.",
                  "default": False},
    },
    changes=True,
    screened=False,
)
async def audio_file(text: str = "", path: str = "", title: str = "", format: str = "", voice: str = "",
                     phone: bool = False) -> str:
    text, path = text.strip(), path.strip()
    if text and path:
        raise ValueError("Give the text to read or a document's path, not both.")
    if not (text or path):
        raise ValueError("Give either the text to read or a document's path.")
    fmt = format or config.AUDIO_FORMAT
    if not render.can_make(fmt):
        how = "brew install ffmpeg" if platform.MAC else "install it with the system's package manager"
        raise ValueError(f"No {fmt} encoder on this computer: {fmt} needs {render.programs(fmt)} ({how})"
                         + (". m4a works without it." if fmt != "m4a" and render.can_make("m4a") else "."))
    voice = voice.strip() or config.tts_voice()
    if not re.fullmatch(r"[a-z]{2}_[a-z0-9]+", voice):
        raise ValueError(f"{voice} isn't a Kokoro voice name; they look like bm_george or af_heart.")
    named = ""
    if path:
        text, named = await asyncio.to_thread(_document, path)
    spoken = render.speakable(text)
    if not spoken:
        raise ValueError("There's nothing in it to read.")
    if len(spoken) > render.MAX_CHARS:
        raise ValueError(f"It's {len(spoken):,} characters; at most {render.MAX_CHARS:,} (about 12 hours of audio) "
                         "go in one file. Split it.")
    name = render.file_name(title or named or _first_words(spoken))
    out = render.free_path(config.AUDIO_DIR, name, fmt)
    speed = config.tts_speed()
    render.work_dir().mkdir(parents=True, exist_ok=True)
    spec = render.work_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json"
    write_atomic(spec, json.dumps({"text": spoken, "out": str(out), "format": fmt, "voice": voice, "speed": speed,
                                   "title": name, "phone": phone}), mode=0o600)
    command = f"{shlex.quote(sys.executable)} -m jarvis.voice.audio_file {shlex.quote(str(spec))}"
    try:  # last line reports the result; no auto-fix
        job = await client.start_command(command, f"the audio file {name}", str(config.ROOT), summarize=False,
                                         auto_fix=False, report=True)
    except Exception:
        spec.unlink(missing_ok=True)
        raise
    seconds = render.seconds_of(spoken, speed)
    return (f"Started job {job['id']}, the audio file {name}: about {render.length(seconds)} of audio, to be saved as "
            f"{out.name} in {render.shown(out.parent)}. Making it takes roughly "
            f"{render.length(max(30.0, seconds / RENDER_SPEED))}"
            + (", and then it goes to the phone" if phone else "")
            + ". It runs on its own; JARVIS says when it's done.")

