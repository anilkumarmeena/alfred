"""Files over the link, in chunks: one side offers a file, the other fetches it piece by piece and checks its hash.

Used for update bundles, a task's git bundles and patches, and synced project files. Offers are kept until fetched
or an hour old; only files the offering side put up can be fetched, by an unguessable id.
"""

import base64
import hashlib
import secrets
import time
from pathlib import Path

from jarvis.node.link import Link, Refused

CHUNK = 1024 * 1024  # bytes per fetch; base64 keeps a message well under link.MAX_MESSAGE
KEEP_SECONDS = 3600


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


class Outbox:
    """Files this side offers; serve `fetch` requests from them."""

    def __init__(self) -> None:
        self.offered: dict[str, tuple[Path, float, bool]] = {}  # id -> (file, when, delete once fetched)

    def offer(self, path: Path, remove: bool = False) -> dict:
        """Offer `path`: returns {"transfer", "size", "sha256"} for the other side to fetch. `remove`: delete the
        file once it has been fetched in full (or the offer lapses)."""
        self._lapse()
        transfer = secrets.token_urlsafe(16)
        self.offered[transfer] = (path, time.time(), remove)
        return {"transfer": transfer, "size": path.stat().st_size, "sha256": digest(path)}

    def _lapse(self) -> None:
        now = time.time()
        for transfer, (path, when, remove) in list(self.offered.items()):
            if now - when > KEEP_SECONDS:
                self._drop(transfer)

    def _drop(self, transfer: str) -> None:
        path, _, remove = self.offered.pop(transfer)
        if remove:
            path.unlink(missing_ok=True)

    async def fetch(self, body: dict) -> dict:
        """Handler: {"transfer", "offset"} -> {"data": base64, "end": bool}."""
        transfer, offset = str(body.get("transfer")), int(body.get("offset") or 0)
        if transfer not in self.offered:
            raise Refused("that file isn't offered any more")
        path = self.offered[transfer][0]
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(CHUNK)
        end = offset + len(data) >= path.stat().st_size
        if end:
            self._drop(transfer)
        return {"data": base64.b64encode(data).decode(), "end": end}


async def fetch(link: Link, offer: dict, into: Path, most: int | None = None, timeout: float = 120) -> Path:
    """Fetch an offered file into `into`, checking size and hash. ValueError if it's over `most` bytes or arrives
    damaged (the partial file is removed)."""
    size = int(offer["size"])
    if most is not None and size > most:
        raise ValueError(f"the file is {size // 1_000_000} MB, over the {most // 1_000_000} MB limit")
    into.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    try:
        with open(into, "wb") as f:
            offset, end = 0, False
            while not end:  # at least once, so even an empty file's offer is let go
                got = await link.call("fetch", {"transfer": offer["transfer"], "offset": offset}, timeout)
                data = base64.b64decode(got["data"])
                f.write(data)
                h.update(data)
                offset += len(data)
                end = bool(got.get("end")) or not data
                if offset > size:
                    raise ValueError("more arrived than was offered")
        if offset != size or h.hexdigest() != offer["sha256"]:
            raise ValueError("the file arrived damaged")
    except BaseException:
        into.unlink(missing_ok=True)
        raise
    return into
