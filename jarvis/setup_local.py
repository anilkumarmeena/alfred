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
  uninstall [--all [--yes]]   remove every service (a node's too) and the service logs folder; --all also deletes
             the install's data, settings, secrets, models and Claude logins and the checkout's build files, after
             you type "delete" (--yes skips that). The code stays.
  clean      empty logs/ (and the service logs), and, unless a task is running, delete the task caches and machine
             transfers left in data/node/spool; safe while the services run

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
from contextlib import closing
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


# ── Uninstall ──

BUILT = (".venv", "web/node_modules", "web/dist")  # what make install builds in the checkout
OWN_LIVEKIT = Path.home() / ".local/bin/livekit-server"  # make livekit-bin's (a Homebrew one is only pointed out)


def uninstall_services() -> list[str]:
    """Stop and remove every service this checkout installed (the four, any under stale labels, a second machine's
    node service) so none comes back at login, and put launchd's service logs back in logs/. Returns their labels."""
    removed = []
    for path in dict.fromkeys([*(installed(name) for name in (*SERVICES, NODE)), *stale()]):
        if not path.exists():
            continue
        if _systemd():
            _systemctl("disable", "--now", path.name)
        else:
            _launchctl("bootout", path)
        path.unlink(missing_ok=True)
        removed.append(path.stem)
    if removed and _systemd():
        _systemctl("daemon-reload")
    _unlink_logs()
    return removed


def _unlink_logs(logs: Path | None = None, service_logs: Path | None = None) -> None:
    """Move the logs launchd wrote in `service_logs` back into `logs`, in place of their links (_link_log)."""
    logs, service_logs = logs or LOGS_DIR, service_logs or SERVICE_LOGS  # read now, so tests can redirect them
    if not service_logs.is_dir():
        return
    for target in sorted(service_logs.glob("*.log")):
        link = logs / target.name
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            logs.mkdir(parents=True, exist_ok=True)
            shutil.move(target, link)
    for link in logs.glob("*.log") if logs.is_dir() else []:
        if link.is_symlink() and link.resolve().is_relative_to(service_logs.resolve()):
            link.unlink()
    shutil.rmtree(service_logs, ignore_errors=True)


def install_files(root: Path = ROOT, home: Path = HOME_DIR, user_home: Path | None = None) -> list[Path]:
    """What uninstall --all deletes: the install's own files (JARVIS_HOME's .env, config.toml, data, secrets, models
    and logs, the checkout's data link, a ~/.jarvis link), what make install built in the checkout, and make
    livekit-bin's livekit-server. Never the checkout's code, or a folder holding it or the user's home."""
    user_home = user_home or Path.home()
    found = [home / name for name in HOME_ITEMS]
    found += [root / name for name in HOME_ITEMS
              if (root / name).is_symlink() and (root / name).resolve() == (home / name).resolve()]
    found += [config.DATA_DIR, config.SECRETS_DIR, *(root / p for p in BUILT), OWN_LIVEKIT]
    if (link := user_home / ".jarvis").is_symlink() and link.resolve() == home.resolve():
        found.append(link)
    keep = [Path("/"), user_home.resolve(), root.resolve()]
    return [path for path in dict.fromkeys(found) if (path.exists() or path.is_symlink())
            and not any(k.is_relative_to(path.resolve()) for k in keep if not path.is_symlink())]


def delete(paths: list[Path], home: Path = HOME_DIR) -> None:
    """Delete each path; a link goes, never what it points to. JARVIS_HOME goes too once it's empty."""
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    if home.is_dir() and not home.is_symlink() and home.resolve() != ROOT.resolve() and not any(home.iterdir()):
        home.rmdir()


def _sign_out_claude() -> None:
    """Sign JARVIS's own Claude logins out, so their keychain entries go with their folders (never the Mac's own)."""
    from jarvis import claude_login
    from jarvis.llm import claude_logins

    for name in claude_logins.named():
        claude_login.sign_out(name)
        print(f"signed out Claude login {name}")


def _still_running() -> bool:
    """Whether a task runner still answers on its socket (make dev or make console, say)."""
    import socket

    try:
        with socket.socket(socket.AF_UNIX) as s:
            return config.RUNNER_SOCKET.exists() and s.connect_ex(str(config.RUNNER_SOCKET)) == 0
    except OSError:
        return False


def _output(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def leftovers(everything: bool) -> list[str]:
    """What uninstalling leaves on this computer that only the user should undo, as hints."""
    hints = []
    tailscale = next((c for c in TAILSCALE_CLIS if shutil.which(c) or os.path.exists(c)), "")
    served = _output(tailscale, "serve", "status") if tailscale else ""
    if any(f":{port}" in served for port in (config.WEB_PORT, 7880)):
        hints.append("Tailscale still serves JARVIS on your tailnet: `tailscale serve reset` clears this computer's "
                     "serve settings (all of them)")
    asleep = (_output("systemctl", "is-enabled", "sleep.target").strip() == "masked" if _systemd()
              else re.search(r"SleepDisabled\s+1", _output("pmset", "-g")))
    if asleep:
        hints.append("Sleep is still off (make awake): `make sleep-normal` turns it back on")
    try:
        from jarvis.node import home, machines

        if machines.load():
            hints.append("Machines paired with this one still try to reach it: run `make uninstall` on each")
        if home.PAIRING_FILE.exists():
            hints.append("The main computer still lists this machine: remove it in Settings › Machines")
    except Exception:  # an unreadable or already deleted install: nothing to say
        pass
    if everything:
        if _output("brew", "list", "--versions", "livekit").strip():
            hints.append("livekit-server came from Homebrew: `brew uninstall livekit` if nothing else uses it")
        hints.append("uv and the Hugging Face cache (~/.cache/huggingface, speech models) are shared with other "
                     "tools, so they stay")
        hints.append(f"The code is still in {_shown(ROOT)}: delete that folder to finish")
    else:
        hints.append("Your data, settings and secrets are kept: `make services-install` brings JARVIS back, "
                     "`make uninstall-all` deletes them too")
    return hints


def uninstall(everything: bool = False, yes: bool = False, ask=input) -> None:
    """Remove JARVIS from this computer: its services and service logs; with `everything`, also its data, settings,
    secrets, models, Claude logins and build files, after the user types "delete" (or `yes`)."""
    if _systemd() and not shutil.which("systemctl"):
        print("systemctl isn't installed, so there are no services to remove")
    else:
        for label in uninstall_services():
            print(f"removed {label}")
    if everything:
        if _still_running():
            sys.exit("JARVIS is still running in the foreground (make dev or make console?): stop it and rerun")
        paths = install_files()
        print("This deletes, for good (links go, not what they point to):")
        for path in paths:
            print(f"  {_shown(path)}")
        if not yes:
            if not sys.stdin.isatty() and ask is input:
                sys.exit('Not deleting anything without a yes: rerun in a terminal, or with YES=1')
            if ask('Type "delete" to go ahead: ').strip() != "delete":
                sys.exit("Nothing deleted; the services stay removed")
        _sign_out_claude()
        hints = leftovers(everything)  # before the files that tell them go
        delete(paths)
        if (ROOT / ".git").exists():
            subprocess.run(["git", "-C", str(ROOT), "worktree", "prune"], check=False)
        print("deleted")
    else:
        hints = leftovers(everything)
    for hint in hints:
        print(f"- {hint}")


def _size(path: Path) -> int:
    """Bytes in a file or folder (a link's target, for a link to a file); 0 if it's gone."""
    try:
        if not path.is_dir() or path.is_symlink():
            return path.stat().st_size if path.is_file() else 0
        return sum(f.lstat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    except OSError:
        return 0


def _tasks_running() -> bool:
    """Whether a task is running (its commands use the task caches; a machine transfer, the spool). True if the task
    database can't say, to be safe."""
    import sqlite3

    from jarvis.storage import database, profile

    if not profile.current().db.exists():
        return False
    try:
        with closing(database.connect()) as db:
            return db.execute("select 1 from tasks where status = 'running' limit 1").fetchone() is not None
    except sqlite3.OperationalError as e:
        return "no such table" not in str(e)  # a fresh install: no tasks yet
    except sqlite3.Error:
        return True


def clean(logs: Path | None = None, service_logs: Path | None = None, data: Path | None = None,
          running: bool | None = None) -> int:
    """Free what JARVIS rebuilds or never reads back: empty logs/ (files are emptied, not removed, as running
    services keep writing to them; folders such as the speech gate's saved clips go), and, unless a task is
    running, the sandboxed commands' build caches and temp files (data/task-cache) and machine transfers left in
    data/node/spool. Never the database, settings, secrets, memory, models or task records. Returns bytes freed."""
    logs, service_logs, data = logs or LOGS_DIR, service_logs or SERVICE_LOGS, data or config.DATA_DIR
    freed, emptied = 0, set()
    for path in [*_children(logs), *(service_logs.glob("*.log") if service_logs.is_dir() else [])]:
        if path.is_dir() and not path.is_symlink():
            freed += _size(path)
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and (target := path.resolve()) not in emptied:
            emptied.add(target)
            freed += _size(target)
            target.write_bytes(b"")
    if running if running is not None else _tasks_running():
        print("A task is running, so its caches and transfers stay: rerun make clean when it's done")
        return freed
    for path in [*_children(data / "task-cache"), *_children(data / "node" / "spool")]:
        freed += _size(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    return freed


def _children(folder: Path) -> list[Path]:
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _clean_command() -> None:
    freed = clean()
    print(f"freed {freed / 1e9:.1f} GB" if freed >= 1e8 else f"freed {freed / 1e6:.1f} MB")


def _uninstall_command() -> None:
    flags = set(sys.argv[2:])
    uninstall(everything="--all" in flags, yes="--yes" in flags)


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
                "install": install_services, "move-home": _move_home_command, "uninstall": _uninstall_command,
                "clean": _clean_command}
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
