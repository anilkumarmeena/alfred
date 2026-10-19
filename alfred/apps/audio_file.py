"""The audio-file job (python -m alfred.apps.audio_file SPEC.json, started by alfred.tools.audio): renders the text
(alfred.media.audio_file) and, when asked, shares the file and pushes a notification that plays it on the phone."""

import asyncio
import sys
from pathlib import Path

from alfred.media import audio_file
from alfred.media.audio_file import length


async def send_to_phone(path: Path, title: str, seconds: float) -> str:
    """Share the file and push a play notification; returns text to append to the result line."""
    from alfred.config import PUSH_ENABLED
    from alfred.notify import push
    from alfred.state import shared_files

    if not PUSH_ENABLED:
        return " Not sent to the phone: notifications are off on the computer (ALFRED_PUSH)."
    token = shared_files.share(path)
    try:
        sent = await push.send_push("Audio ready", f"{title}: {length(seconds)}. Tap to listen.",
                                    shared_files.link(token, path.name), f"file-{token[:12]}")
    except Exception as e:
        return f" Couldn't send it to the phone: {type(e).__name__}."
    return (" Sent to the phone." if sent else
            " Not sent to the phone: no phone has Alfred's notifications on.")


def deliver(path: Path, title: str, seconds: float) -> str:
    return asyncio.run(send_to_phone(path, title, seconds))


if __name__ == "__main__":
    sys.exit(audio_file.main(sys.argv[1:], deliver))
