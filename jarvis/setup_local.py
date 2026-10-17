"""Local setup, safe to rerun: uv run python -m jarvis.setup_local [--install | start | stop | status | labels]

  (default)  fill missing LiveKit keys, passcode and vault master key in .env (mode 600), move keys into the vault
             and the passcode into logins (make passcode shows it again); ask once for the projects folder; write secrets/livekit.yaml
  --install  also (re)install and start the services: launchd agents from deploy/*.plist on macOS, systemd user
             units from deploy/systemd/*.service on Linux
  start      load installed services, installing missing ones
  install    rewrite and reload every service (no setup steps)
  stop       unload services (they stay installed)
  status     print each service's state
  labels     print the service labels
  node-install / node-stop   on a second machine: its one service, which runs python -m jarvis.node serve
  move-home  move .env, data, secrets, models and logs out of the checkout into JARVIS_HOME (default ~/.jarvis);
             run with the services stopped (make home-move does both)

Services this checkout installed under old labels (e.g. a changed JARVIS_SERVICE_PREFIX) are stale: stop unloads them;
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

from jarvis import config
from jarvis.auth import credentials, logins, vault
from jarvis.config import HOME_DIR, LOGS_DIR, ROOT, SECRETS_DIR
from jarvis.core import platform
from jarvis.core.files import write_atomic

ENV = HOME_DIR / ".env"
LIVEKIT_CONFIG = SECRETS_DIR / "livekit.yaml"
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
SYSTEMD_USER = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"
# launchd opens a service's log itself, before the program runs, and macOS denies it inside ~/Desktop, ~/Documents
# and ~/Downloads whatever access the program has. So on macOS the service logs live here, linked from logs/.
SERVICE_LOGS = Path.home() / "Library/Logs" / config.SERVICE_PREFIX
# deploy/<name>.plist -> <label>.plist; deploy/systemd/<name>.service -> <label>.service
SERVICES = config.SERVICE_NAMES
MANAGER = platform.SERVICE_MANAGER  # "launchd" or "systemd"
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


# ── Services: launchd on macOS, systemd user units on Linux ─────


def livekit_server() -> str:
    """livekit-server on PATH, else where make livekit-bin puts it (Linux, or macOS without Homebrew)."""
    return shutil.which("livekit-server") or str(Path.home() / ".local/bin/livekit-server")


def _fill(template: Path, name: str) -> str:
    """`template` with ${LABEL}, ${ROOT} (the code), ${JARVIS_HOME} (the install's settings and data), ${HOME},
    ${NODE} (current node's bin dir plus ":", or ""), ${LIVEKIT} (the livekit-server binary) and ${LOGS} (where launchd
    writes service logs) substituted."""
    node = shutil.which("node")
    return Template(template.read_text()).substitute(
        LABEL=config.service_label(name), ROOT=ROOT, JARVIS_HOME=HOME_DIR, HOME=Path.home(),
        NODE=f"{Path(node).parent}:" if node else "", LIVEKIT=livekit_server(), LOGS=SERVICE_LOGS)


def service_plist(name: str) -> str:
    """deploy/<name>.plist, filled in."""
    return _fill(ROOT / "deploy" / f"{name}.plist", name)


def service_unit(name: str) -> str:
    """deploy/systemd/<name>.service, filled in."""
    return _fill(ROOT / "deploy" / "systemd" / f"{name}.service", name)


def _systemd() -> bool:
    return MANAGER == "systemd"


def installed(name: str) -> Path:
    """~/Library/LaunchAgents/<label>.plist, or ~/.config/systemd/user/<label>.service on Linux."""
    if _systemd():
        return SYSTEMD_USER / f"{config.service_label(name)}.service"
    return LAUNCH_AGENTS / f"{config.service_label(name)}.plist"


def _service(path: Path) -> str:
    """Service name from a plist or unit: com.jarvis.agent.plist -> "agent"."""
    return path.stem.rpartition(".")[2]


def _write(name: str) -> Path:
    """Write the service's plist or unit, creating its folder and logs/ if needed."""
    dest = installed(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if not _systemd():
        _link_log(name)
    dest.write_text(service_unit(name) if _systemd() else service_plist(name))
    return dest


def _link_log(name: str) -> None:
    """Make logs/<name>.log a link to the service log in SERVICE_LOGS, moving an existing log's history there."""
    link, target = LOGS_DIR / f"{name}.log", SERVICE_LOGS / f"{name}.log"
    SERVICE_LOGS.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target:
        return
    if link.is_file() and not link.is_symlink() and not target.exists():
        shutil.move(link, target)
    link.unlink(missing_ok=True)
    link.symlink_to(target)


def _log_file(path: Path) -> object:
    """Where an installed plist or unit sends its output; None if unreadable."""
    try:
        if _systemd():
            found = re.search(r"^StandardOutput=append:(.+)$", path.read_text(), re.M)
            return found and found.group(1).strip()
        return plistlib.loads(path.read_bytes()).get("StandardOutPath", "")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def stale() -> list[Path]:
    """Plists or units from this checkout under unused labels: named *.<service> and logging into this checkout's
    logs/."""
    current = {installed(name) for name in SERVICES}
    folder, pattern = (SYSTEMD_USER, "*.service") if _systemd() else (LAUNCH_AGENTS, "*.plist")
    found = []
    for path in sorted(folder.glob(pattern)):
        if path in current or _service(path) not in SERVICES:
            continue
        logs = _log_file(path)
        if isinstance(logs, str) and Path(logs).parent in (LOGS_DIR, SERVICE_LOGS):
            found.append(path)
    return found


def _launchctl(action: str, plist: Path) -> str:
    """launchctl bootstrap/bootout in the GUI domain; "" on success, else the error. (Legacy load/unload exit 0 even
    on failure.)"""
    done = subprocess.run(["launchctl", action, f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
    return "" if done.returncode == 0 else (done.stderr or done.stdout or f"exit {done.returncode}").strip()


def _systemctl(*args: str) -> str:
    """systemctl --user; "" on success, else the error."""
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except OSError as e:
        return str(e)
    return "" if done.returncode == 0 else (done.stderr or done.stdout or f"exit {done.returncode}").strip()


def _require_systemd() -> None:
    """Exit with a hint when the systemd user manager can't be reached (WSL2 without systemd, no session bus)."""
    error = "systemctl isn't installed" if not shutil.which("systemctl") else _systemctl("show-environment")
    if error:
        sys.exit(f"JARVIS can't reach systemd's user manager ({error}), so it can't install its services.\n"
                 "- On WSL2: add\n    [boot]\n    systemd=true\n  to /etc/wsl.conf and run `wsl --shutdown` from Windows.\n"
                 "- Over SSH or su: log in as yourself so XDG_RUNTIME_DIR is set, and install dbus-user-session; "
                 "after installing it, log out and back in.\n"
                 "Or run everything in the foreground with `make dev`.")


def _unload(path: Path) -> str:
    """Stop a service from its installed plist or unit; "" on success, else the error."""
    return _systemctl("stop", path.name) if _systemd() else _launchctl("bootout", path)


def _running(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0


def _start(name: str, tries: int = 5, restart: bool = False) -> bool:
    """Start a service; prints the error on failure. launchd: bootstrap, retrying while a just-stopped one shuts down
    ("5: Input/output error"), and already running counts as started. systemd: enable (back at login) and start, or
    restart to pick up a rewritten unit."""
    label, error = config.service_label(name), ""
    if _systemd():
        unit = installed(name).name
        if not (error := _systemctl("enable", unit) or _systemctl("restart" if restart else "start", unit)):
            print(f"started {label}")
            return True
    else:
        for attempt in range(tries):
            if not (error := _launchctl("bootstrap", installed(name))) or _running(label):
                print(f"started {label}")
                return True
            if attempt < tries - 1:
                time.sleep(2)
    print(f"couldn't start {label}: {error}", file=sys.stderr)
    return False


def _retire_stale() -> None:
    """Unload stale services and delete their plists or units so they don't return at login."""
    for path in stale():
        if _systemd():
            _systemctl("disable", "--now", path.name)
        else:
            _launchctl("bootout", path)
        path.unlink(missing_ok=True)
        print(f"removed {path.stem}, now {config.service_label(_service(path))}")


# The desktop session's variables, so notifications, the clipboard and screenshots work from a service
DESKTOP_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _import_desktop_env() -> None:
    """Hand the user manager this session's desktop variables (desktops usually do; a terminal-only login doesn't)."""
    if found := [k for k in DESKTOP_ENV if os.environ.get(k)]:
        _systemctl("import-environment", *found)


def _linger_hint() -> None:
    """Without lingering, systemd stops user services at logout and starts them only at login."""
    user = os.environ.get("USER") or str(os.getuid())
    try:
        done = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True,
                              text=True)
    except OSError:
        return
    if done.returncode == 0 and (done.stdout or "").strip() == "no":
        print(f"To keep JARVIS running after you log out and start it at boot, run once: "
              f"sudo loginctl enable-linger {user}")


def install_services() -> None:
    """Retire stale labels, rewrite and reload every plist or unit. Exits 1 if any service fails to start."""
    if _systemd():
        _require_systemd()
    _retire_stale()
    started = True
    if _systemd():
        for name in SERVICES:
            _write(name)
        _systemctl("daemon-reload")
        _import_desktop_env()
        for name in SERVICES:
            started &= _start(name, restart=True)
        _linger_hint()
    else:
        for name in SERVICES:
            _launchctl("bootout", installed(name))
            _write(name)
            started &= _start(name)
    if not started:
        sys.exit(1)


def start_services() -> None:
    """Retire stale labels, install missing plists or units and load all services. Exits 1 if any fails to start."""
    if _systemd():
        _require_systemd()
    _retire_stale()
    missing = [name for name in SERVICES if not installed(name).exists()]
    for name in missing:
        _write(name)
    if _systemd():
        _systemctl("daemon-reload")
        _import_desktop_env()
    started = True
    for name in SERVICES:
        started &= _start(name)
    if _systemd():
        _linger_hint()
    if not started:
        sys.exit(1)


def stop_services() -> None:
    """Unload current and stale services; plists and units stay (start removes stale ones)."""
    if _systemd():
        _require_systemd()
    for path in [installed(name) for name in SERVICES] + stale():
        if path.exists() and not _unload(path):
            print(f"stopped {path.stem}")


NODE = "node"  # a second machine's one service (deploy/node.plist, deploy/systemd/node.service), apart from SERVICES


HOME_ITEMS = (".env", "config.toml", "data", "secrets", "models", "logs")  # what an install keeps (jarvis.config.files)


def move_home(dest: Path | None = None, root: Path = ROOT) -> list[str]:
    """Move an install's own files from the checkout into `dest` (JARVIS_HOME, else ~/.jarvis), so the checkout holds
    only code. Leaves a data link behind for the absolute paths kept in the database, and repairs the git worktrees
    tasks made in data/worktrees. Returns what moved; refuses before moving anything if `dest` already has one (empty
    folders don't count: importing jarvis.config makes data and secrets) or one in the checkout is a link elsewhere."""
    dest = dest or Path(os.environ.get("JARVIS_HOME", "").strip() or Path.home() / ".jarvis").expanduser()
    present = [name for name in HOME_ITEMS if (root / name).exists() or (root / name).is_symlink()]
    moved_already = [name for name in present
                     if (root / name).is_symlink() and (root / name).resolve() == (dest / name).resolve()]
    if links := [name for name in present if (root / name).is_symlink() and name not in moved_already]:
        raise SystemExit(f"{', '.join(links)} in {root} link elsewhere; move what they point to by hand")
    items = [name for name in present if name not in moved_already]
    if clash := [name for name in items if (dest / name).exists() and not _empty_dir(dest / name)]:
        raise SystemExit(f"{dest} already has {', '.join(clash)}; move or remove it first")
    dest.mkdir(parents=True, exist_ok=True)
    for name in items:
        if _empty_dir(dest / name):
            (dest / name).rmdir()
        shutil.move(root / name, dest / name)
    if "data" in items:
        (root / "data").symlink_to(dest / "data")
        worktrees = dest / "data" / "worktrees"
        if worktrees.is_dir() and (root / ".git").exists():
            found = [str(p) for p in worktrees.iterdir() if (p / ".git").is_file()]
            if found:
                subprocess.run(["git", "-C", str(root), "worktree", "repair", *found], check=False)
    return items


def _empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def remember_home(dest: Path, user_home: Path | None = None) -> bool:
    """Link ~/.jarvis to a home elsewhere, so commands and services run without JARVIS_HOME still find it
    (jarvis.config.files.home). False if ~/.jarvis is already there."""
    own = (user_home or Path.home()) / ".jarvis"
    if own.exists() or own.is_symlink() or own.resolve() == dest.resolve():
        return False
    own.symlink_to(dest.resolve())
    return True


def _move_home_command() -> None:
    moved = move_home()
    print("moved", ", ".join(moved) or "nothing")
    if (given := os.environ.get("JARVIS_HOME", "").strip()) and remember_home(Path(given).expanduser()):
        print(f"linked ~/.jarvis to {given}")


def install_node() -> None:
    """On a second machine: (re)write and start its one service. Exits 1 if it fails to start."""
    if _systemd():
        _require_systemd()
        _write(NODE)
        _systemctl("daemon-reload")
        started = _start(NODE, restart=True)
    else:
        _launchctl("bootout", installed(NODE))
        _write(NODE)
        started = _start(NODE)
    if not started:
        sys.exit(1)


def stop_node() -> None:
    if (path := installed(NODE)).exists() and not _unload(path):
        print(f"stopped {path.stem}")


def properties(text: str) -> dict[str, str]:
    """systemctl show output (Key=Value lines) as a dict."""
    return {key: value for key, found, value in (line.partition("=") for line in text.splitlines()) if found}


def status() -> list[tuple[str, str]]:
    """(label, "running (pid N)" | "stopped (exit N)" | "not loaded") for each service."""
    found = []
    if _systemd():
        for name in SERVICES:
            done = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState,ActiveState,MainPID,ExecMainStatus",
                                   installed(name).name], capture_output=True, text=True)
            props = properties(done.stdout or "") if done.returncode == 0 else {}
            if props.get("LoadState", "not-found") == "not-found":
                state = "not loaded"
            elif props.get("ActiveState") in ("active", "reloading"):
                state = f"running (pid {props.get('MainPID', '?')})"
            else:
                state = f"stopped (exit {props.get('ExecMainStatus', '?')})"
            found.append((config.service_label(name), state))
        return found
    done = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    rows = {cols[2]: cols for line in (done.stdout or "").splitlines() if len(cols := line.split()) == 3}
    for name in SERVICES:
        pid, code, _ = rows.get(label := config.service_label(name), (None, None, None))
        found.append((label, "not loaded" if pid is None else f"stopped (exit {code})" if pid == "-"
                      else f"running (pid {pid})"))
    return found


def print_status() -> None:
    if _systemd():
        _require_systemd()
    for label, state in status():
        print(f"  {label:<26} {state}")


def main() -> None:
    services = {"start": start_services, "stop": stop_services, "status": print_status,
                "labels": lambda: print(" ".join(config.service_label(name) for name in SERVICES)),
                "node-install": install_node, "node-stop": stop_node,
                "install": install_services, "move-home": _move_home_command}
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("-h", "--help", "help"):
        return print(__doc__.strip())
    if command := services.get(arg):
        return command()
    if not ENV.exists():
        ENV.parent.mkdir(parents=True, exist_ok=True)
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
        # Also kept encrypted in the vault: make passcode shows it again
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
