"""Git helpers for the link: the commit JARVIS runs, and bundles of its code for a machine's self-update.

Only committed code travels: a bundle from the main computer's checked-out commit, never uncommitted changes.
"""

from pathlib import Path

from jarvis.core import proc


async def _git(repo: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    return await proc.run_try("git", "-C", str(repo), *args, timeout=timeout)


async def head(repo: Path) -> str:
    """The commit checked out in `repo`, or "" if it isn't a git checkout."""
    code, out = await _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() if code == 0 else ""


async def has(repo: Path, commit: str) -> bool:
    return bool(commit) and (await _git(repo, "cat-file", "-e", f"{commit}^{{commit}}"))[0] == 0


async def make_update(repo: Path, have: str, into: Path) -> Path:
    """Bundle `repo`'s HEAD into `into`: only what's new since `have` when that's an ancestor, else everything."""
    into.parent.mkdir(parents=True, exist_ok=True)
    since = []
    if have and await has(repo, have) and (await _git(repo, "merge-base", "--is-ancestor", have, "HEAD"))[0] == 0:
        since = [f"^{have}"]
    commit = await head(repo)
    # A branch name for the bundle's tip, so it can be fetched by name; the commit itself is checked after
    ref = "refs/jarvis-update/main"
    code, out = await _git(repo, "update-ref", ref, commit)
    if code:
        raise RuntimeError(f"couldn't mark the commit to send: {out}"[:300])
    try:
        code, out = await _git(repo, "bundle", "create", str(into), ref, *since, timeout=600)
    finally:
        await _git(repo, "update-ref", "-d", ref)
    if code:
        raise RuntimeError(f"couldn't bundle the code: {out}"[:300])
    return into


async def fetch_update(repo: Path, bundle: Path, commit: str) -> None:
    """Fetch an update bundle into `repo` and check it holds `commit`. RuntimeError if not."""
    code, out = await _git(repo, "fetch", "-q", "--no-tags", str(bundle), "refs/jarvis-update/main:refs/jarvis-update/main",
                           timeout=600)
    if code:
        raise RuntimeError(f"the update bundle didn't fetch: {out}"[:300])
    if await head_of(repo, "refs/jarvis-update/main") != commit:
        raise RuntimeError("the update bundle holds a different commit than was sent")


async def head_of(repo: Path, ref: str) -> str:
    code, out = await _git(repo, "rev-parse", "--verify", "-q", ref)
    return out.strip() if code == 0 else ""


async def dirty(repo: Path) -> bool:
    """Whether tracked files have uncommitted changes (an update would overwrite them)."""
    code, out = await _git(repo, "status", "--porcelain", "--untracked-files=no")
    return code != 0 or bool(out.strip())


async def check_out(repo: Path, commit: str) -> None:
    """Check `commit` out, detached. RuntimeError if git refuses."""
    code, out = await _git(repo, "checkout", "-q", "--detach", commit)
    if code:
        raise RuntimeError(f"couldn't check out {commit[:7]}: {out}"[:300])
