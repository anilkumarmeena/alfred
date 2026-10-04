"""Starts one MCP server for a Claude session:
`python -m jarvis.integrations.mcp_launch NAME [--read-only] [--wait=SECONDS] [--probe]`.

The CLI runs this, as the stdio command jarvis.integrations.mcp_servers gives it, so the MCP config on the CLI's command line
names only the server, never a secret. This is the one place a server's secrets are decrypted (jarvis.core.vault, with the
master key from .env, since the CLI's environment has it blanked): they go into the server's own environment, along
with its settings from the database. The master key and JARVIS's other .env settings aren't passed on. Task sessions
start this with every .env setting blank, which config takes as unset. With --read-only, the server's read-only
arguments and settings are added, for research tasks.

An HTTP server is reached the same way, through a bridge from stdio to its URL (jarvis.integrations.mcp_bridge), which
adds the header with its key, or its signed-in token, to each request.

Either way this process stays between the CLI and the server as a relay (jarvis.integrations.mcp_relay): each tool is
marked read-only as JARVIS sorted it, so the CLI runs reads side by side; a tool the server runs as an MCP task is
called as one; and with --wait (the voice brain's), a call still running after that many seconds carries on in the
background as a task of the runner's, the relay outliving the session until it ends.

--probe starts a server that's off, or whose tools aren't sorted yet, to list them as it marks them itself, with no
relay (mcp_servers.offered); JARVIS uses it while connecting one. Nothing it offers is used then.
"""

import asyncio
import os
import sys
from shutil import which

from jarvis.core import vault
from jarvis.integrations import mcp_servers
from jarvis.safety import host

WAIT = "--wait="  # a call's seconds before it carries on in the background (config.CONNECTION_WAIT)

def command(server: mcp_servers.Server, read_only: bool) -> tuple[list[str], dict[str, str]]:
    """The server's command line and environment. Raises ValueError, or vault.VaultError, if it can't start."""
    if server.transport != "stdio":
        raise ValueError(f"{server.name} is an HTTP server, reached through the bridge.")
    if not (program := which(server.command)):
        raise ValueError(f"{server.name}: {server.command} isn't installed.")
    argv = [program, *server.args, *(server.read_only_args if read_only else [])]
    # Not JARVIS's own settings and keys (.env), which the voice brain's CLI inherits and task sessions blank
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
