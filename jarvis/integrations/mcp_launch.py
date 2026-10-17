"""How one MCP server is started for a Claude session (its argv and environment, secrets decrypted from the vault);
the process that does it is jarvis.apps.mcp_launch."""

from shutil import which

from jarvis.auth import vault
from jarvis.integrations import mcp_servers
from jarvis.safety import host


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


if __name__ == "__main__":  # the old command path, for sessions started before the launcher moved
    import sys

    from jarvis.apps import mcp_launch

    mcp_launch.main(sys.argv[1:])
