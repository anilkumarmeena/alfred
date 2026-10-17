"""Control panel actions: Connections (MCP servers) and the system's services and vault."""

import asyncio
import re
import shutil
import time
from dataclasses import replace

from jarvis import config, setup_local
from jarvis.auth import vault
from jarvis.integrations import mcp_servers
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import activity

# ── Connections and system ──────────────────────────────────────


SECRET_MOST = 8000  # max key length


def _server(s: mcp_servers.Server) -> dict:
    by_access: dict[str, int] = {}
    for spec in s.tools.values():
        by_access[spec.access] = by_access.get(spec.access, 0) + 1
    return {"name": s.name, "label": s.label, "enabled": s.enabled, "reviewed": s.reviewed,
            "command": s.url or " ".join([s.command, *s.args]), "tools": by_access,
            "tool_list": [{"name": t, "access": spec.access} for t, spec in sorted(s.tools.items())],
            "secrets": [{"var": var, "set": vault.has(name)} for var, name in s.secret_env.items()],
            "addresses": [{"var": var, "value": url} for var, url in mcp_servers.addresses(s).items()],
            "sign_in": s.sign_in, "signed_in": s.sign_in and vault.has(s.oauth_secret),
            "source": s.source, "verified": s.verified, "version": s.version, "research": s.for_research,
            "problem": mcp_servers.missing(s, reviewed=False) if s.enabled or not s.reviewed else ""}


def _servers() -> list[dict]:
    return [_server(s) for s in mcp_servers.load()]


def _connection(body: dict) -> mcp_servers.Server:
    return mcp_servers.need(_text(body, "name", 40))


@action
async def servers(_: dict) -> list[dict]:
    """MCP servers: state, provenance, tools by access, and which secrets are set (never values)."""
    return await asyncio.to_thread(_servers)


def _switch_server(body: dict) -> list[dict]:
    server = _connection(body)
    on = bool(body.get("on"))
    if on and not server.reviewed:
        raise ValueError(f"{server.label}'s tools haven't been sorted yet: ask JARVIS to review them, and your yes "
                         "switches it on.")
    mcp_servers.save(replace(server, enabled=on))
    return _servers()


@action
async def server_switch(body: dict) -> list[dict]:
    """Enable/disable a connection: {"name", "on"}. Unreviewed ones can't be enabled."""
    return await asyncio.to_thread(_switch_server, body)


def _set_secret(body: dict) -> list[dict]:
    server = _connection(body)
    var = _text(body, "var", 64)
    if var not in server.secret_env:
        raise ValueError(f"{server.label} takes no {var}.")
    value = str(body.get("value", "")).strip()
    if not value or len(value) > SECRET_MOST or "\n" in value:
        raise ValueError("Paste the key on one line.")
    try:
        vault.ensure()
        vault.put(server.secret_env[var], value)
    except vault.VaultError as e:
        raise ValueError(str(e)) from None
    log.warning("the panel set %s's %s", server.name, var)  # name only, never the value
    return _servers()


@action
async def server_secret(body: dict) -> list[dict]:
    """Store a connection secret: {"name", "var", "value"}. Write-only."""
    return await asyncio.to_thread(_set_secret, body)


def _clear_secret(body: dict) -> list[dict]:
    server = _connection(body)
    var = _text(body, "var", 64)
    if var not in server.secret_env:
        raise ValueError(f"{server.label} takes no {var}.")
    vault.remove(server.secret_env[var])
    return _servers()


@action
async def server_secret_clear(body: dict) -> list[dict]:
    """Clear a connection secret: {"name", "var"}."""
    return await asyncio.to_thread(_clear_secret, body)


def _set_address(body: dict) -> dict:
    server = _connection(body)
    var, url = _text(body, "var", 64), _text(body, "value", 500)
    changed = mcp_servers.readdress(server, var, url)
    if changed == server:
        return {"forgot": [], "servers": _servers()}
    mcp_servers.save(changed)
    forgot = []
    if mcp_servers.host_of(server.env.get(var, "")) != mcp_servers.host_of(url):
        # Its keys were for the old server; keeping them would let any login send them to a server it picked
        forgot = [v for v, secret in server.secret_env.items() if vault.remove(secret)]
    activity.add(f"Pointed {server.label} at {url or 'its default server'} in the control panel"
                 + (f", forgetting its {', '.join(forgot)}." if forgot else "."))
    return {"forgot": forgot, "servers": _servers()}


@action
async def server_address(body: dict) -> dict:
    """Set a connection's server address: {"name", "var", "value"} ('' for its default). Moving it to another host
    forgets its keys."""
    return await asyncio.to_thread(_set_address, body)


@action
async def server_sign_in(body: dict) -> dict:
    """OAuth sign-in for a connection in JARVIS's browser on the computer; waits up to SIGN_IN_MINUTES: {"name"}."""
    from jarvis.integrations import mcp_oauth
    from jarvis.integrations.browser import BROWSER, Unavailable

    server = await asyncio.to_thread(_connection, body)
    if not server.sign_in:
        raise ValueError(f"{server.label} doesn't sign in.")
    try:
        said = await mcp_oauth.sign_in(server, lambda url, back: BROWSER.sign_in(url, back, mcp_oauth.SIGN_IN_MINUTES))
    except TimeoutError:
        raise ValueError("The sign-in wasn't finished in time.") from None
    except (mcp_oauth.SignInError, Unavailable) as e:
        raise ValueError(str(e)) from None
    finally:
        await BROWSER.close()
    await asyncio.to_thread(activity.add, said)
    return {"said": said, "servers": await asyncio.to_thread(_servers)}


@action
async def server_impact(body: dict) -> dict:
    """What removing a connection would affect, shown before confirming: {"name"}."""
    from jarvis.tools.connections import impact

    return {"impact": await asyncio.to_thread(lambda: impact(_connection(body)))}


@action
async def server_remove(body: dict) -> list[dict]:
    """Remove a connection with its keys and sign-in: {"name"}."""
    server = await asyncio.to_thread(_connection, body)
    await asyncio.to_thread(mcp_servers.remove, server.name)
    await asyncio.to_thread(activity.add, f"Removed {server.label} in the control panel.")
    return await asyncio.to_thread(_servers)


async def _launchd(label: str) -> dict:
    """Service state from `launchctl list`: running (with start time), stopped (exit code) or not loaded."""
    out = ""
    try:
        proc = await asyncio.create_subprocess_exec("launchctl", "list", label, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
        out = raw.decode(errors="replace") if proc.returncode == 0 else ""
    except (OSError, TimeoutError):
        pass
    name = SERVICE_NAMES.get(label.rsplit(".", 1)[-1], label)
    if not out:
        return {"name": name, "state": "not loaded"}
    pid = re.search(r'"PID" = (\d+);', out)
    if not pid:
        status = re.search(r'"LastExitStatus" = (-?\d+);', out)
        return {"name": name, "state": "stopped", "exit": int(status.group(1)) if status else None}
    return {"name": name, "state": "running", "since": await _started(int(pid.group(1)))}


async def _started(pid: int) -> float | None:
    """Process start time from ps etime ([[dd-]hh:]mm:ss)."""
    try:
        proc = await asyncio.create_subprocess_exec("ps", "-o", "etime=", "-p", str(pid),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, TimeoutError):
        return None
    days, _, clock = raw.decode().strip().rpartition("-")
    parts = [int(p) for p in clock.split(":") if p.isdigit()]
    if not parts:
        return None
    seconds = sum(v * m for v, m in zip(reversed(parts), (1, 60, 3600))) + int(days or 0) * 86400
    return time.time() - seconds


async def _systemd(label: str) -> dict:
    """Service state from `systemctl --user show`: running (with start time), stopped (exit code) or not loaded."""
    out = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "show", "-p", "LoadState,ActiveState,ExecMainStatus,ExecMainStartTimestampMonotonic",
            f"{label}.service", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
        out = raw.decode(errors="replace") if proc.returncode == 0 else ""
    except (OSError, TimeoutError):
        pass
    name = SERVICE_NAMES.get(label.rsplit(".", 1)[-1], label)
    props = setup_local.properties(out)
    if props.get("LoadState", "not-found") == "not-found":
        return {"name": name, "state": "not loaded"}
    if props.get("ActiveState") not in ("active", "reloading"):
        status = props.get("ExecMainStatus", "")
        return {"name": name, "state": "stopped", "exit": int(status) if status.lstrip("-").isdigit() else None}
    # Monotonic microseconds: the same clock as time.monotonic() on Linux, and no date string to parse
    started = props.get("ExecMainStartTimestampMonotonic", "")
    since = time.time() - (time.monotonic() - int(started) / 1e6) if started.isdigit() and int(started) else None
    return {"name": name, "state": "running", "since": since}


def _vault() -> dict:
    try:
        vault.verify()
    except vault.VaultError as e:
        return {"state": "problem", "problem": str(e)}
    return {"state": "ok", "secrets": len(vault.names())}


@action
async def system(_: dict) -> dict:
    systemd = setup_local.MANAGER == "systemd"
    found, probe = (shutil.which("systemctl"), _systemd) if systemd else (shutil.which("launchctl"), _launchd)
    labels = [config.service_label(name) for name in setup_local.SERVICES]
    services = await asyncio.gather(*(probe(label) for label in labels)) if found else []
    return {"services": list(services), "vault": await asyncio.to_thread(_vault)}
