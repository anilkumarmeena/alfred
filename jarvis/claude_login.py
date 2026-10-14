"""Signing JARVIS's Claude logins in (core.claude_logins), from the control panel (Settings › Claude logins) or with
uv run python -m jarvis.claude_login [login | check] [NAME]

  login (default)  sign NAME in (without one, the default login: yours), check that the services can use it, then
                   restart them if they're running
  check            only the check

On macOS the CLI keeps a login in the login keychain, which the services (launchd, the desktop's "Aqua" session)
read. From any other session (SSH, a VS Code tunnel) the CLI can't reach the keychain until it's unlocked there, and
then saves the login to a file only that session's CLIs read: `claude` there works while the services keep the old
login until it can't be refreshed ("OAuth session expired"). So login unlocks the keychain first, and check makes a
one-word call the way the services would: as a launchd job when not in the desktop session.

The panel signs in through the web service, which runs in the desktop session, so no unlock is needed: SignIns runs
`claude auth login` in a pseudo-terminal there, hands its sign-in link to the page, and types the code Claude's page
shows (pasted into the panel) back into it.
"""

import fcntl
import json
import os
import plistlib
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.core import claude_logins, platform
from jarvis.core.config import CLAUDE_CLI, DATA_DIR, FAST_MODEL, ROOT, SERVICE_PREFIX

LOGIN_KEYCHAIN = Path.home() / "Library/Keychains/login.keychain-db"
CHECK_LABEL = f"{SERVICE_PREFIX}.claude-check"
CHECK_SECONDS = 90
CHECK_PROMPT = "Reply with just: ok"
STATUS_SECONDS = 20
SIGN_IN_SECONDS = 15 * 60  # an unfinished panel sign-in is dropped after this
LINK_SECONDS = 30  # for the CLI to show its sign-in link
FINISH_SECONDS = 60  # for it to finish once the code is typed
LINK = re.compile(r"https://\S+/oauth/authorize\?\S+(?=\s)")  # whole: up to the space after it
PROMPT = re.compile(r"Paste code here if prompted\s*>?")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


def cli() -> str:
    """The CLI the services run, as an absolute path (launchd's PATH is short)."""
    found = shutil.which(CLAUDE_CLI or "claude")
    if not found:
        raise FileNotFoundError(f"Claude Code CLI not found ({CLAUDE_CLI or 'claude on PATH'}); install it or set "
                                "JARVIS_CLAUDE_CLI.")
    return found


def desktop_session() -> bool:
    """Whether this runs in the Mac's desktop session, the one the services run in."""
    done = subprocess.run(["launchctl", "managername"], capture_output=True, text=True)
    return done.stdout.strip() == "Aqua"


def status(name: str) -> dict:
    """`claude auth status` for a login: loggedIn, email, orgName, subscriptionType ({} if it can't tell)."""
    try:
        done = subprocess.run([cli(), "auth", "status"], env=os.environ | claude_logins.env(name), capture_output=True,
                              text=True, timeout=STATUS_SECONDS, cwd=DATA_DIR)
        found = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


# ── The check: a one-word call, as the services would make it ──


def _reply(name: str) -> str:
    """The check's reply, run here."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            done = subprocess.run([cli(), "-p", CHECK_PROMPT, "--model", FAST_MODEL], capture_output=True, text=True,
                                  env=os.environ | claude_logins.env(name), timeout=CHECK_SECONDS, cwd=tmp)
        except subprocess.TimeoutExpired:
            return "no reply in time"
    return (done.stdout + done.stderr).strip()


def _launchd_reply(name: str) -> str:
    """The check's reply, run as a launchd job in the desktop session."""
    with tempfile.TemporaryDirectory() as tmp:
        out, plist = Path(tmp) / "out.txt", Path(tmp) / f"{CHECK_LABEL}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": CHECK_LABEL, "RunAtLoad": True, "WorkingDirectory": tmp,
            "ProgramArguments": [cli(), "-p", CHECK_PROMPT, "--model", FAST_MODEL],
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **claude_logins.env(name)},
            "StandardOutPath": str(out), "StandardErrorPath": str(out)}))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{CHECK_LABEL}"], capture_output=True)  # a stuck earlier one
        started = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
        if started.returncode != 0:
            return f"couldn't start it as a launchd job: {(started.stderr or started.stdout).strip()}"
        try:
            for _ in range(CHECK_SECONDS):
                time.sleep(1)
                state = subprocess.run(["launchctl", "print", f"{domain}/{CHECK_LABEL}"], capture_output=True,
                                       text=True).stdout
                if "state = running" not in state:
                    break
        finally:
            subprocess.run(["launchctl", "bootout", f"{domain}/{CHECK_LABEL}"], capture_output=True)
        return out.read_text(errors="replace").strip() if out.exists() else ""


def check(name: str = claude_logins.DEFAULT) -> tuple[bool, str]:
    """Whether JARVIS's services can use a login, and what to say; kept for the panel (claude_logins.note)."""
    try:
        reply = _launchd_reply(name) if platform.MAC and not desktop_session() else _reply(name)
    except FileNotFoundError as e:
        reply = str(e)
    ok = reply.lower().rstrip(".") == "ok"
    said = ("JARVIS's services can use this login." if ok
            else f"JARVIS's services can't use this login: {reply.splitlines()[-1] if reply else 'no reply'}")
    claude_logins.note(name, ok, said)
    return ok, said


# ── Signing in from the panel ──


@dataclass
class SignIn:
    name: str
    process: subprocess.Popen
    fd: int  # the pseudo-terminal's controlling end
    new: bool  # its folder was made for it: forgotten again unless it signs in
    started: float = field(default_factory=time.time)
    seen: str = ""  # what the CLI showed, without terminal codes

    def read(self, until: re.Pattern | None, seconds: float) -> re.Match | None:
        """Collect output for up to `seconds`: until `until` shows (its match), or until the CLI ends (None)."""
        import select
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:  # the CLI ended and closed its side
                    chunk = b""
                if not chunk:
                    return None
                self.seen += ANSI.sub("", chunk.decode(errors="replace")).replace("\r", "")
                if until and (found := until.search(self.seen)):
                    return found
            elif self.process.poll() is not None:
                return None
        return None

    def last(self) -> str:
        """The CLI's last line, to say why it stopped."""
        lines = [l.strip(" >") for l in PROMPT.sub("", self.seen).splitlines() if l.strip(" >")]
        return lines[-1] if lines else "nothing"

    def end(self, signed_in: bool = False) -> None:
        """Stop the CLI if it's still waiting; a new login that didn't sign in is forgotten."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.new and not signed_in:
            claude_logins.remove(self.name)


class SignIns:
    """Panel sign-ins in progress, by state (each a `claude auth login` waiting for its code)."""

    def __init__(self) -> None:
        self._open: dict[str, SignIn] = {}
        self._lock = threading.Lock()

    def start(self, name: str, email: str = "") -> dict:
        """Start signing `name` in ("" = the default login): {"state", "url"} for the page."""
        self._expire()
        new = bool(name) and name not in claude_logins.named()
        if name:
            claude_logins.make(name)
        controller, terminal = os.openpty()
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))  # wide: the link on one line
        try:
            process = subprocess.Popen(
                [cli(), "auth", "login", "--claudeai", *(["--email", email] if email else [])],
                stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True, cwd=DATA_DIR,
                env=os.environ | claude_logins.env(name) | {"BROWSER": "/usr/bin/true", "TERM": "xterm-256color"})
        except OSError:
            os.close(controller)
            if new:
                claude_logins.remove(name)
            raise
        finally:
            os.close(terminal)
        sign_in = SignIn(name, process, controller, new)
        link = sign_in.read(LINK, LINK_SECONDS)
        if not link:
            sign_in.end()
            raise RuntimeError(f"Claude's sign-in didn't start: {sign_in.last()}")
        state = secrets.token_urlsafe(16)
        with self._lock:
            self._open[state] = sign_in
        return {"state": state, "url": link.group(0)}

    def finish(self, state: str, code: str) -> tuple[bool, str]:
        """Type the code Claude's page showed into the sign-in, then check the login: (worked, what to say)."""
        with self._lock:
            sign_in = self._open.pop(state, None)
        if not sign_in:
            raise ValueError("That sign-in is over: start it again.")
        done = None
        try:
            os.write(sign_in.fd, code.strip().encode())
            time.sleep(0.3)  # a separate Enter, so the code isn't taken as one long paste ending in a newline
            os.write(sign_in.fd, b"\r")
            sign_in.seen = ""
            sign_in.read(None, FINISH_SECONDS)
            try:
                done = sign_in.process.wait(5)  # its terminal can close a moment before it exits
            except subprocess.TimeoutExpired:
                done = None
            if done is None:
                return False, f"Claude didn't finish signing in: {sign_in.last()}"
            if done != 0:
                return False, f"Claude didn't sign in: {sign_in.last()}"
        finally:
            sign_in.end(signed_in=done == 0)
        return check(sign_in.name)

    def cancel(self, state: str) -> None:
        with self._lock:
            sign_in = self._open.pop(state, None)
        if sign_in:
            sign_in.end()

    def _expire(self) -> None:
        with self._lock:
            old = [s for s, sign_in in self._open.items() if time.time() - sign_in.started > SIGN_IN_SECONDS]
            ended = [self._open.pop(s) for s in old]
        for sign_in in ended:
            sign_in.end()


signins = SignIns()


def sign_out(name: str) -> None:
    """Sign one of JARVIS's logins out (its keychain entry) and forget its folder."""
    claude_logins.known(name)
    try:
        subprocess.run([cli(), "auth", "logout"], env=os.environ | claude_logins.env(name), capture_output=True,
                       timeout=STATUS_SECONDS, cwd=DATA_DIR)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the folder goes anyway; a keychain entry left behind is never read
    claude_logins.remove(name)


# ── The command line ──


def unlock_keychain() -> None:
    """Unlock the login keychain for this session; asks for the Mac password."""
    print("Not in the Mac's desktop session, so the login keychain has to be unlocked here first,\n"
          "or the login won't reach JARVIS's services.")
    if subprocess.run(["security", "unlock-keychain", str(LOGIN_KEYCHAIN)]).returncode != 0:
        sys.exit("Couldn't unlock the login keychain; the login would only reach this session. Not logging in.")


def restart_services() -> None:
    """Restart the services if any is running, so sessions already open pick up the login."""
    states = subprocess.run([sys.executable, "-m", "jarvis.setup_local", "status"], capture_output=True, text=True,
                            cwd=ROOT).stdout
    if "running" not in states:
        return
    print("Restarting JARVIS's services to pick up the login...")
    subprocess.run(["make", "--no-print-directory", "-C", str(ROOT), "services-restart"])


def login(name: str) -> None:
    if name:
        claude_logins.make(name)
    if platform.MAC and not desktop_session():
        unlock_keychain()
    if subprocess.run([cli(), "auth", "login"], env=os.environ | claude_logins.env(name)).returncode != 0:
        sys.exit("Claude login didn't finish.")
    ok, said = check(name)
    print(said)
    if not ok:
        sys.exit("Log in again from the Mac's own desktop or the control panel (Settings › Claude logins), or unlock "
                 "the keychain in this session and rerun.")
    if claude_logins.active() != name:
        place = claude_logins.order().index(name) + 1
        print(f"It's number {place} in the priority order, so JARVIS runs on {claude_logins.active() or 'the default'} "
              "for now: change the order in Settings › Claude logins.")
    restart_services()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        return print(__doc__.strip())
    command = args[0] if args and args[0] in ("login", "check") else "login"
    rest = args[1:] if args and args[0] in ("login", "check") else args
    try:
        name = claude_logins.valid(rest[0]) if rest and rest[0] not in ("", "default") else claude_logins.DEFAULT
        if command == "check":
            ok, said = check(claude_logins.known(name))
            print(said)
            sys.exit(0 if ok else 1)
        login(name)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
