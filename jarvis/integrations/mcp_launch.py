"""Starts one MCP server for a Claude session; the only place its secrets are decrypted.

Usage: python -m jarvis.integrations.mcp_launch NAME [--read-only] [--wait=SECONDS] [--probe]

  --read-only    add the server's read-only args and settings (sign-in checks and watches)
  --wait=SECONDS voice brain: a call still running after SECONDS continues in the background as a task
  --probe        start a disabled or unsorted server bare (no relay) just to list its tools

The CLI's MCP config names only the server, never a secret. Secrets go into the server's own environment; the master
key and JARVIS's .env settings are never passed on. HTTP servers are reached via mcp_bridge. Except with --probe,
this process stays between CLI and server as a relay (mcp_relay) that marks read-only tools and runs MCP tasks.
"""

import asyncio
import os
import sys
from shutil import which

from jarvis.core import vault
from jarvis.integrations import mcp_servers
from jarvis.safety import host

WAIT = "--wait="  # seconds before a call continues in the background (config.CONNECTION_WAIT)

def command(server: mcp_servers.Server, read_only: bool) -> tuple[list[str], dict[str, str]]:
    """Server argv and env. Raises ValueError or vault.VaultError if it can't start."""
    if server.transport != "stdio":
        raise ValueError(f"{server.name} is an HTTP server, reached through the bridge.")
    if not (program := which(server.command)):
        raise ValueError(f"{server.name}: {server.command} isn't installed.")
    argv = [program, *server.args, *(server.read_only_args if read_only else [])]
    # Drop JARVIS's own .env settings and keys
    env = {k: v for k, v in host.scrubbed_env().items() if k != vault.VAR} | server.env
    env |= server.read_only_env if read_only else {}
    for var, secret in server.secret_env.items():
        env[var] = vault.get(secret)
    return argv, env


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    server = mcp_servers.get(argv[0])
    flags = argv[1:]
    probe = "--probe" in flags
    wait = next((float(f.removeprefix(WAIT)) for f in flags if f.startswith(WAIT)), 0.0)
    if server is None or not (probe or server.enabled and server.reviewed):
        sys.exit(f"There's no enabled MCP server {argv[0]}.")
    try:
        how = None if server.transport == "http" else command(server, "--read-only" in flags)
        if probe and how:
            os.execvpe(how[0][0], *how)
        elif probe:
            from jarvis.integrations import mcp_bridge

            sys.exit(asyncio.run(mcp_bridge.serve(server)))
        else:
            from jarvis.integrations import mcp_relay

            if wait:
                mcp_relay.detach()
            sys.exit(asyncio.run(mcp_relay.serve(server, wait, how)))
    except (ValueError, vault.VaultError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
