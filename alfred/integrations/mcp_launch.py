"""How one MCP server is started for a Claude session (its argv and environment, secrets decrypted from the vault);
the process that does it is alfred.apps.mcp_launch.

A pinned `npx -y PACKAGE@VERSION` server runs as node on the copy npx already installed (`direct`): no npx process
beside it and no registry lookup at each start. Until npx has installed that version, or for any other npx form, npx
runs it as given, which installs it for next time."""

import json
import os
from pathlib import Path
from shutil import which

from alfred.auth import vault
from alfred.integrations import mcp_servers
from alfred.safety import host

_QUIET = {"-y", "--yes", "-q", "--quiet"}  # npx flags that don't change what runs


def _npx_cache() -> Path:
    """Where npx keeps the packages it installs (npm's cache folder, _npx)."""
    return Path(os.environ.get("npm_config_cache") or Path.home() / ".npm") / "_npx"


def direct(args: list[str]) -> tuple[list[str], Path] | None:
    """npx `args` as node on its installed package: ([node, entry point, *its args], that install's .bin folder), or
    None to run npx: not installed at exactly that version yet, another npx form, or a program that isn't node's."""
    rest = list(args)
    while rest and rest[0] in _QUIET:
        rest.pop(0)
    name, _, version = (rest[0] if rest else "").rpartition("@")
    if not name or name.startswith("-") or not version or not (node := which("node")):
        return None
    found = [(manifest, modules) for modules in _npx_cache().glob("*/node_modules")
             if (manifest := modules / name / "package.json").is_file()]
    for manifest, modules in sorted(found, key=lambda f: f[0].stat().st_mtime, reverse=True):  # newest install first
        try:
            package = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        if package.get("version") != version.removeprefix("v"):
            continue
        bins = package.get("bin")
        if isinstance(bins, dict):  # as npx picks: its only program, else the one named after the package
            bins = next(iter(bins.values())) if len(bins) == 1 else bins.get(name.rpartition("/")[2])
        if not isinstance(bins, str):
            return None
        entry = (manifest.parent / bins).resolve()
        try:
            with entry.open("rb") as f:
                first = f.readline()
        except OSError:  # a broken install: npx mends it
            return None
        if not entry.is_relative_to(manifest.parent.resolve()) or first.startswith(b"#!") and b"node" not in first:
            return None
        return [node, str(entry), *rest[1:]], modules / ".bin"
    return None


def command(server: mcp_servers.Server, read_only: bool) -> tuple[list[str], dict[str, str]]:
    """Server argv and env. Raises ValueError or vault.VaultError if it can't start."""
    if server.transport != "stdio":
        raise ValueError(f"{server.name} is an HTTP server, reached through the bridge.")
    if not (program := which(server.command)):
        raise ValueError(f"{server.name}: {server.command} isn't installed.")
    args = [*server.args, *(server.read_only_args if read_only else [])]
    argv = [program, *args]
    # Drop Alfred's own .env settings and keys
    env = {k: v for k, v in host.scrubbed_env().items() if k != vault.VAR} | server.env
    env |= server.read_only_env if read_only else {}
    if Path(server.command).name == "npx" and (found := direct(args)):
        argv, bins = found
        env["PATH"] = os.pathsep.join([str(bins), *filter(None, [env.get("PATH")])])  # its own programs, as npx has
    for var, secret in server.secret_env.items():
        env[var] = vault.get(secret)
    return argv, env


if __name__ == "__main__":  # the old command path, for sessions started before the launcher moved
    import sys

    from alfred.apps import mcp_launch

    mcp_launch.main(sys.argv[1:])
