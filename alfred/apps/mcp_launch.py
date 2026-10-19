"""Starts one MCP server for a Claude session; the only place its secrets are decrypted.

Usage: python -m alfred.apps.mcp_launch NAME [--read-only] [--wait=SECONDS] [--probe]

  --read-only    add the server's read-only args and settings (sign-in checks and watches)
  --wait=SECONDS voice brain: a call still running after SECONDS continues in the background as a task
  --probe        start a disabled or unsorted server bare (no relay) just to list its tools

The CLI's MCP config names only the server, never a secret. Secrets go into the server's own environment; the master
key and Alfred's .env settings are never passed on. HTTP servers are reached via mcp_bridge. Except with --probe,
this process stays between CLI and server as a relay (mcp_relay) that marks read-only tools and runs MCP tasks, handing
calls that run long to the task runner (alfred.tasks.client). The relay starts a local server only at its first call,
answering the CLI's tool list till then from its last start.
"""

import asyncio
import os
import sys

from alfred.auth import vault
from alfred.integrations import mcp_servers
from alfred.integrations.mcp_launch import command

WAIT = "--wait="  # seconds before a call continues in the background (config.CONNECTION_WAIT)


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    server = mcp_servers.get(argv[0])
    flags = argv[1:]
    probe = "--probe" in flags
    wait = next((float(f.removeprefix(WAIT)) for f in flags if f.startswith(WAIT)), 0.0)
    if server is None or not (probe or server.enabled and server.reviewed):
        sys.exit(f"There's no enabled MCP server {argv[0]}.")
    read_only = "--read-only" in flags
    try:
        how = None if server.transport == "http" else command(server, read_only)
        if probe and how:
            os.execvpe(how[0][0], *how)
        elif probe:
            from alfred.integrations import mcp_bridge

            sys.exit(asyncio.run(mcp_bridge.serve(server)))
        else:
            from alfred.integrations import mcp_relay

            if wait:
                mcp_relay.detach()
            from alfred.tasks import client

            sys.exit(asyncio.run(mcp_relay.serve(server, wait, how, runner=client, read_only=read_only)))
    except (ValueError, vault.VaultError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
