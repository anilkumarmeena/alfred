"""Local setup, safe to rerun: uv run python -m jarvis.setup_local [--install | start | stop | labels]

  (default)  fill missing LiveKit keys, passcode and vault master key in .env (mode 600), move keys into the vault
             and the passcode into logins (shown once); ask once for the projects folder; write secrets/livekit.yaml
  --install  also (re)install and start the launchd services from deploy/*.plist
  start      load installed services, installing missing ones
  stop       unload services (they stay installed)
  labels     print the service labels

Plists this checkout installed under old labels (e.g. a changed JARVIS_SERVICE_PREFIX) are stale: stop unloads them;
start and --install remove them before loading the current ones.
"""

import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from string import Template

from dotenv import load_dotenv

from jarvis.core import config, credentials, logins, vault
from jarvis.core.config import ROOT, SECRETS_DIR
from jarvis.core.files import write_atomic

ENV = ROOT / ".env"
LIVEKIT_CONFIG = SECRETS_DIR / "livekit.yaml"
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
SERVICES = config.SERVICE_NAMES  # deploy/<name>.plist -> <label>.plist
TAILSCALE_CLIS = ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]


def _env_get(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _env_set(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def tailscale_ip() -> str:
    for cli in TAILSCALE_CLIS:
        if shutil.which(cli) or os.path.exists(cli):
            out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.split()[0]
    return ""


def _shown(path: Path) -> str:
    """Path for .env, as ~/... when under home."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def ask_projects_folder(text: str, ask=input) -> str:
    """Set JARVIS_NEW_PROJECTS_DIR (and JARVIS_TASK_READ_DIRS if unset) in `text`, asking on a TTY, else
    config.projects_home(). No-op if JARVIS_NEW_PROJECTS_DIR is already set in .env or the environment."""
    if _env_get(text, "JARVIS_NEW_PROJECTS_DIR") or os.environ.get("JARVIS_NEW_PROJECTS_DIR", "").strip():
        return text
    folder = default = _shown(config.projects_home())
    if sys.stdin.isatty():
        answer = ask(f"Where are your projects kept? New projects go there, and tasks may read them [{default}]: ")
        folder = answer.strip() or default
    if not Path(folder).expanduser().is_dir():
        print(f"{folder} doesn't exist yet; it's made when the first new project goes there.")
    text = _env_set(text, "JARVIS_NEW_PROJECTS_DIR", folder)
    if not _env_get(text, "JARVIS_TASK_READ_DIRS") and "JARVIS_TASK_READ_DIRS" not in os.environ:
        text = _env_set(text, "JARVIS_TASK_READ_DIRS", folder)
    return text


# ── launchd services ────────────────────────────────────────────


def service_plist(name: str) -> str:
    """deploy/<name>.plist with ${LABEL}, ${ROOT}, ${HOME} and ${NODE} (current node's bin dir plus ":", or "")
    substituted."""
    node = shutil.which("node")
    return Template((ROOT / "deploy" / f"{name}.plist").read_text()).substitute(
        LABEL=config.service_label(name), ROOT=ROOT, HOME=Path.home(), NODE=f"{Path(node).parent}:" if node else "")


def installed(name: str) -> Path:
    """~/Library/LaunchAgents/<label>.plist."""
    return LAUNCH_AGENTS / f"{config.service_label(name)}.plist"


def _service(plist: Path) -> str:
    """Service name from a plist: com.jarvis.agent.plist -> "agent"."""
    return plist.stem.rpartition(".")[2]


def _write(name: str) -> Path:
    """Write the service plist, creating LaunchAgents/ and logs/ if needed."""
    dest = installed(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    dest.write_text(service_plist(name))
    return dest


def stale() -> list[Path]:
    """Plists from this checkout under unused labels: named *.<service> and logging into this checkout's logs/."""
    current = {installed(name) for name in SERVICES}
    found = []
    for plist in sorted(LAUNCH_AGENTS.glob("*.plist")):
        if plist in current or _service(plist) not in SERVICES:
            continue
        try:
            logs = plistlib.loads(plist.read_bytes()).get("StandardOutPath", "")
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(logs, str) and Path(logs).parent == ROOT / "logs":
            found.append(plist)
    return found


def _launchctl(action: str, plist: Path) -> str:
    """launchctl bootstrap/bootout in the GUI domain; "" on success, else the error. (Legacy load/unload exit 0 even
    on failure.)"""
    done = subprocess.run(["launchctl", action, f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
    return "" if done.returncode == 0 else (done.stderr or done.stdout or f"exit {done.returncode}").strip()


def _running(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0


def _start(name: str, tries: int = 5) -> bool:
    """Bootstrap a service, retrying while a just-stopped one shuts down ("5: Input/output error"). Already running
    counts as started; prints the error on failure."""
    label, error = config.service_label(name), ""
    for attempt in range(tries):
        if not (error := _launchctl("bootstrap", installed(name))) or _running(label):
            print(f"started {label}")
            return True
        if attempt < tries - 1:
            time.sleep(2)
    print(f"couldn't start {label}: {error}", file=sys.stderr)
    return False


def _retire_stale() -> None:
    """Unload stale services and delete their plists so they don't return at login."""
    for plist in stale():
        _launchctl("bootout", plist)
        plist.unlink(missing_ok=True)
        print(f"removed {plist.stem}, now {config.service_label(_service(plist))}")


def install_services() -> None:
    """Retire stale labels, rewrite and reload every plist. Exits 1 if any service fails to start."""
    _retire_stale()
    started = True
    for name in SERVICES:
        _launchctl("bootout", installed(name))
        _write(name)
        started &= _start(name)
    if not started:
        sys.exit(1)


def start_services() -> None:
    """Retire stale labels, install missing plists and load all services. Exits 1 if any fails to start."""
    _retire_stale()
    started = True
    for name in SERVICES:
        if not installed(name).exists():
            _write(name)
        started &= _start(name)
    if not started:
        sys.exit(1)


def stop_services() -> None:
    """Unload current and stale services; plists stay (start removes stale ones)."""
    for plist in [installed(name) for name in SERVICES] + stale():
        if plist.exists() and not _launchctl("bootout", plist):
            print(f"stopped {plist.stem}")


def main() -> None:
    services = {"start": start_services, "stop": stop_services,
                "labels": lambda: print(" ".join(config.service_label(name) for name in SERVICES))}
    if command := services.get(sys.argv[1] if len(sys.argv) > 1 else ""):
        return command()
    if not ENV.exists():
        shutil.copy(ROOT / ".env.example", ENV)
    text = ENV.read_text()
    # First-setup keys and passcode go into .env, then adopt() below moves them to the vault and logins
    if not _env_get(text, "LIVEKIT_API_KEY") and not credentials.get(credentials.LIVEKIT_KEY):
        text = _env_set(text, "LIVEKIT_API_KEY", "jarvis" + secrets.token_hex(4))
    if not _env_get(text, "LIVEKIT_API_SECRET") and not credentials.get(credentials.LIVEKIT_SECRET):
        text = _env_set(text, "LIVEKIT_API_SECRET", secrets.token_urlsafe(32))
    new_passcode = "" if _env_get(text, logins.VAR) or logins.ready() else f"{secrets.randbelow(10**6):06d}"
    if new_passcode:
        text = _env_set(text, logins.VAR, new_passcode)
    if not _env_get(text, vault.VAR) and not os.environ.get(vault.VAR) and not config.MASTER_KEY:
        text = _env_set(text, vault.VAR, vault.new_key())  # vault master key
    text = ask_projects_folder(text)
    write_atomic(ENV, text, mode=0o600)
    load_dotenv(ENV)  # so adopt() sees the new values

    for label in credentials.adopt():
        print(f"Moved the {label} from .env into the vault.")
    if login := logins.adopt():
        print(f"The passcode in .env is now {login}'s login, kept hashed in secrets/; it's out of .env.")
    ip = write_livekit_config()
    print(f"LiveKit config written. Media address: {ip or 'local network (Tailscale not running)'}")
    if new_passcode:
        # Shown once; only its hash is kept
        print(f"Phone passcode for {logins.first_name()}: {new_passcode}")

    if "--install" in sys.argv:
        install_services()


def write_livekit_config() -> str:
    """Write secrets/livekit.yaml (mode 600) with the LiveKit keys; the panel rewrites it when keys change. Returns
    the Tailscale media IP, or ""."""
    ip = tailscale_ip()
    node_ip = f"  node_ip: {ip}\n" if ip else ""
    write_atomic(LIVEKIT_CONFIG, (
        "# Generated by jarvis.setup_local. Signalling stays on localhost (Tailscale Serve proxies it);\n"
        "# media uses one UDP port on the Tailscale address.\n"
        "port: 7880\n"
        "bind_addresses: [127.0.0.1]\n"
        "rtc:\n"
        "  tcp_port: 7881\n"
        "  udp_port: 7882\n"
        "  use_external_ip: false\n"
        f"{node_ip}"
        "keys:\n"
        f"  {credentials.get(credentials.LIVEKIT_KEY)}: {credentials.get(credentials.LIVEKIT_SECRET)}\n"
        "logging:\n  level: info\n"
    ), mode=0o600)
    return ip


if __name__ == "__main__":
    main()
