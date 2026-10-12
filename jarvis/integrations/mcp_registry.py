"""Search the official MCP registry (registry.modelcontextprotocol.io) and turn a hit into a mcp_servers entry.

A registry entry (server.json) has a verified reverse-DNS name, a version, and ways to run it: hosted `remotes` (https,
maybe with a header key) or local `packages` (npm via npx, PyPI via uvx, OCI via docker). `ways` splits them into
runnable and not (with reasons); `build` makes the entry: version pinned, every setting given or defaulted, secrets
as vault references entered in the control panel, never by voice. `unlisted` builds an unverified entry from a web
search hit (package or https URL plus the page it was found on).

    uv run python -m jarvis.integrations.mcp_registry QUERY     print what a search finds
"""

import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
from shutil import which
from urllib.parse import quote, urlsplit

import httpx

from jarvis.core import vault
from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import ENV_NAME, Server

REGISTRY = os.environ.get("JARVIS_MCP_REGISTRY", "https://registry.modelcontextprotocol.io").rstrip("/")
TIMEOUT = 20
SHOWN = 8  # results returned
FETCHED = 60  # results fetched before ranking
DESCRIBED = 300  # description chars returned
_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RUNNERS = {"npm": "npx", "pypi": "uvx", "oci": "docker"}
KINDS = {"npm": "an npm package", "pypi": "a Python package", "oci": "a container image"}


class RegistryError(Exception):
    """Registry unreachable or entry missing; the message says which."""


# ── Searching ───────────────────────────────────────────────────

async def _get(path: str, params: dict | None = None, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        r = await client.get(REGISTRY + path, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise RegistryError(f"The MCP registry can't be reached right now ({type(e).__name__}).") from None
    finally:
        if own:
            await client.aclose()
    if r.status_code == 404:
        raise RegistryError("The MCP registry has no such server.")
    if r.status_code != 200:
        raise RegistryError(f"The MCP registry answered {r.status_code}.")
    return r.json()


def _status(item: dict) -> str:
    return ((item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}).get("status", "active")


def _score(query: str, server: dict) -> int:
    """Match rank: own name/title, then publisher namespace, then substring; runnable entries break ties."""
    q = query.lower().strip()
    name = server.get("name", "").lower()
    namespace, _, short = name.partition("/")
    title = (server.get("title") or "").lower()
    score = 0
    if q in (short, title) or short == f"{q}-mcp" or short == f"{q}-mcp-server" or short == "mcp":
        score += 4
    if q in namespace.split("."):
        score += 3  # publisher, e.g. com.notion/...
    if q in short or q in title:
        score += 2
    if ways(server)[0]:
        score += 1  # runnable
    return score


async def search(query: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Latest active entries for `query`, best first. The registry matches names only, so multi-word queries fall
    back to single words."""
    words = [query.strip()] + sorted({w for w in re.split(r"\W+", query) if len(w) > 2} - {query.strip()},
                                     key=len, reverse=True)
    found: dict[str, dict] = {}
    for word in words[:3]:
        reply = await _get("/v0/servers", {"search": word, "version": "latest", "limit": FETCHED}, client)
        for item in reply.get("servers") or []:
            server = item.get("server") or {}
            if server.get("name") and _status(item) == "active":
                found.setdefault(server["name"], server)
        if found:
            break
    return sorted(found.values(), key=lambda s: -_score(query, s))[:SHOWN]


async def fetch(name: str, client: httpx.AsyncClient | None = None) -> dict:
    """Latest entry for `name`, its registry name (namespace/server)."""
    item = await _get(f"/v0/servers/{quote(name.strip(), safe='')}/versions/latest", client=client)
    if _status(item) != "active":
        raise RegistryError(f"The MCP registry marks {name} as {_status(item)}.")
    return item.get("server") or {}


# ── Ways to run ─────────────────────────────────────────────────

@dataclass
class Need:
    """A setting or secret a Way requires."""
    var: str
    description: str = ""
    secret: bool = False
    required: bool = True
    default: str = ""
    choices: list[str] = field(default_factory=list)

    def brief(self) -> dict:
        out = {"name": self.var, "what": self.description[:200]}
        out |= {"secret": True} if self.secret else {}
        out |= {} if self.required else {"optional": True}
        out |= {"default": self.default} if self.default else {}
        out |= {"choices": self.choices} if self.choices else {}
        return out


@dataclass
class Way:
    """One way to run a server: hosted remote or local package."""
    kind: str  # remote, npm, pypi, oci
    spec: dict
    needs: list[Need] = field(default_factory=list)
    problem: str = ""  # why it can't run, or ''

    def brief(self, index: int) -> dict:
        out = {"way": index, "runs": self.said()}
        out |= {"needs": [n.brief() for n in self.needs]} if self.needs else {}
        out |= {"cant": self.problem} if self.problem else {}
        return out

    def said(self) -> str:
        if self.kind == "remote":
            return f"hosted at {urlsplit(self.spec.get('url', '')).hostname or 'its URL'}"
        return f"on this computer, as {KINDS[self.kind]} ({self.spec.get('identifier', '')})"


def _needs_of(items: list, secret_default: bool = False) -> list[Need]:
    """Needs from server.json environmentVariables, headers or variables."""
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(Need(item["name"], str(item.get("description") or ""), bool(item.get("isSecret", secret_default)),
                        bool(item.get("isRequired", False)), str(item.get("default") or item.get("value") or ""),
                        [str(c) for c in item.get("choices") or []]))
    return out


def _variables(spec: dict) -> list[Need]:
    return _needs_of([{"name": k} | v for k, v in (spec.get("variables") or {}).items() if isinstance(v, dict)])


def ways(server: dict) -> tuple[list[Way], list[Way]]:
    """(runnable, not runnable) ways, hosted first; each unrunnable one has a `problem`."""
    can, cant = [], []
    for remote in server.get("remotes") or []:
        if not isinstance(remote, dict):
            continue
        way = Way("remote", remote)
        needs = _variables(remote)
        for header in remote.get("headers") or []:
            if isinstance(header, dict) and header.get("name"):
                needs += _variables(header)
                if not _VAR.search(str(header.get("value") or "")):
                    needs.append(Need(header["name"], str(header.get("description") or ""),
                                      bool(header.get("isSecret")), bool(header.get("isRequired", True)),
                                      "" if header.get("isSecret") else str(header.get("value") or "")))
        way.needs = needs
        if remote.get("type") != "streamable-http":
            way.problem = f"it's reached over {remote.get('type', 'an unknown transport')}, which JARVIS doesn't speak"
        elif not str(remote.get("url", "")).startswith("https://"):
            way.problem = "its URL isn't https"
        (cant if way.problem else can).append(way)
    for package in server.get("packages") or []:
        if not isinstance(package, dict):
            continue
        kind = package.get("registryType", "")
        way = Way(kind if kind in RUNNERS else "other", package)
        way.needs = _needs_of(package.get("environmentVariables"))
        for arg in [*(package.get("runtimeArguments") or []), *(package.get("packageArguments") or [])]:
            if isinstance(arg, dict):
                way.needs += _variables(arg)
                if arg.get("isRequired") and not (arg.get("value") or arg.get("default")):
                    way.needs.append(Need(arg.get("name") or arg.get("valueHint") or "argument",
                                          str(arg.get("description") or ""), bool(arg.get("isSecret"))))
        if kind not in RUNNERS:
            way.problem = f"it's {kind or 'a kind of package'} JARVIS can't run"
        elif (package.get("transport") or {}).get("type", "stdio") != "stdio":
            way.problem = "the package serves over HTTP itself, which JARVIS doesn't start"
        elif not package.get("identifier"):
            way.problem = "it doesn't name its package"
        elif why := _secret_on_command_line(package):
            way.problem = why
        elif not which(RUNNERS[kind]):
            way.problem = f"{RUNNERS[kind]} isn't installed on this computer"
        (cant if way.problem else can).append(way)
    return can, cant


def _secret_on_command_line(package: dict) -> str:
    """Reason if the package needs a secret on argv (visible to every process), else ''."""
    for arg in [*(package.get("runtimeArguments") or []), *(package.get("packageArguments") or [])]:
        if not isinstance(arg, dict):
            continue
        secret_vars = {k for k, v in (arg.get("variables") or {}).items() if isinstance(v, dict) and v.get("isSecret")}
        value = str(arg.get("value") or "")
        if package.get("registryType") == "oci" and arg.get("name") == "-e" and "=" in value:
            continue  # docker -e VAR=...: build passes it via env instead
        if arg.get("isSecret") or secret_vars & set(_VAR.findall(value)):
            return "it takes its key on the command line, where other programs could read it"
    return ""


# ── Building the entry ──────────────────────────────────────────

def slug(text: str, taken: list[str] = ()) -> str:
    """Short unused snake_case server name from a title or registry name."""
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    base = re.sub(r"_?(mcp|server)(_|$)", "_", base).strip("_") or "server"
    if not base[0].isalpha():
        base = "s_" + base
    base = re.sub(r"_+", "_", base)[:28].strip("_")
    if base in mcp_servers.RESERVED:
        base += "_mcp"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def label_of(server: dict) -> str:
    """Spoken label: title, else the last part of the name."""
    title = " ".join(str(server.get("title") or "").split())
    if title and len(title) <= 40:
        return title
    short = server.get("name", "").rpartition("/")[2]
    short = re.sub(r"[-_]?(mcp|server)([-_]|$)", " ", short, flags=re.IGNORECASE)
    return (" ".join(w.capitalize() for w in re.split(r"[-_\s]+", short) if w) or "Server")[:40]


def _secret_name(server_name: str, var: str) -> str:
    name = f"{server_name}.{re.sub(r'[^a-z0-9_.-]+', '_', var.lower()).strip('_.-')}"[:64]
    if not vault.NAME.fullmatch(name):
        raise ValueError(f"{var} can't name a secret.")
    return name


def _env_var(name: str) -> str:
    """Header/argument name as an env var: Authorization -> AUTHORIZATION."""
    var = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").upper() or "SECRET"
    return var if ENV_NAME.fullmatch(var) else "V_" + var


class Building:
    """`build` state: given settings, missing needs, secret_env."""

    def __init__(self, name: str, settings: dict[str, str]) -> None:
        self.name, self.settings = name, {k: str(v) for k, v in (settings or {}).items()}
        self.missing: list[Need] = []
        self.secret_env: dict[str, str] = {}

    def value(self, need: Need) -> str:
        """Given value or default; a required need with neither is recorded as missing."""
        got = self.settings.get(need.var, need.default)
        if need.choices and got and got not in need.choices:
            raise ValueError(f"{need.var} is one of {', '.join(need.choices)}, not {got}.")
        if not got and need.required:
            self.missing.append(need)
        return got

    def secret(self, var: str) -> str:
        """Register a secret in secret_env; returns its variable name for a `{VAR}` placeholder."""
        var = var if ENV_NAME.fullmatch(var) else _env_var(var)
        self.secret_env[var] = _secret_name(self.name, var)
        return var

    def fill(self, text: str, variables: dict) -> str:
        """Fill `{variables}` in text; secrets stay as `{VAR}` for the launcher or bridge."""
        def one(m: re.Match) -> str:
            spec = variables.get(m.group(1)) if isinstance(variables.get(m.group(1)), dict) else {}
            need = Need(m.group(1), str(spec.get("description") or ""), bool(spec.get("isSecret")),
                        bool(spec.get("isRequired", True)), str(spec.get("default") or ""))
            return "{" + self.secret(need.var) + "}" if need.secret else self.value(need)
        return _VAR.sub(one, text)


def _arguments(args: list, b: Building, oci: bool) -> tuple[list[str], dict[str, str]]:
    """Package arguments as (argv, env). Docker `-e VAR=value` becomes `-e VAR` with the value (or secret) in env, so
    it never appears on the command line."""
    argv, env = [], {}
    for arg in args or []:
        if not isinstance(arg, dict):
            continue
        variables = arg.get("variables") or {}
        raw = str(arg.get("value") or arg.get("default") or "")
        if not raw and arg.get("isRequired"):
            raw = b.value(Need(arg.get("name") or arg.get("valueHint") or "argument", str(arg.get("description") or "")))
        if not raw and not arg.get("isRequired") and arg.get("type") == "named":
            continue  # optional flag with no value
        if oci and arg.get("name") == "-e" and "=" in raw:
            var, _, template = raw.partition("=")
            filled = b.fill(template, variables)
            if m := re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", filled):
                b.secret_env.pop(m.group(1), None)
                b.secret_env[var] = _secret_name(b.name, var)  # secret injected as VAR itself
            elif filled:
                env[var] = filled
            else:
                continue  # optional and unset
            argv += ["-e", var]
            continue
        value = b.fill(raw, variables)
        if "{" in value and _VAR.search(value):
            raise ValueError("It takes its key on the command line, where other programs could read it.")
        if arg.get("type") == "named":
            argv += [arg["name"], value] if value else [arg["name"]]
        elif value:
            argv.append(value)
    return argv, env


def build(found: dict, way: Way, name: str, label: str, settings: dict[str, str] | None = None) -> tuple[Server, list[Need]]:
    """Entry for running registry entry `found` via `way`, disabled and unreviewed. Returns it with the non-secret
    settings still missing; secrets are entered in the control panel."""
    if way.problem:
        raise ValueError(f"JARVIS can't run {label} that way: {way.problem}.")
    b = Building(name, settings or {})
    spec = way.spec
    server = Server(name, label, enabled=False, source=found.get("name", ""), verified=True,
                    version=str(found.get("version") or spec.get("version") or ""), for_research=True, reviewed=False)
    if way.kind == "remote":
        server.transport = "http"
        server.url = b.fill(str(spec["url"]), spec.get("variables") or {})
        for header in spec.get("headers") or []:
            if not isinstance(header, dict) or not header.get("name"):
                continue
            value = str(header.get("value") or "")
            if _VAR.search(value):
                filled = b.fill(value, header.get("variables") or {})
            elif header.get("isSecret"):
                var = b.secret(_env_var(header["name"]))
                filled = ("Bearer {%s}" % var) if header["name"].lower() == "authorization" else "{%s}" % var
            else:
                filled = b.value(Need(header["name"], str(header.get("description") or ""), False,
                                      bool(header.get("isRequired", True)), value))
            if filled:
                server.headers[header["name"]] = filled
        server.secret_env = b.secret_env
        return server, b.missing
    package, version = str(spec["identifier"]), str(spec.get("version") or found.get("version") or "")
    env: dict[str, str] = {}
    for need in _needs_of(spec.get("environmentVariables")):
        if need.secret:
            if need.required or need.var in b.settings:
                b.secret(need.var)
        elif value := b.value(need):
            env[need.var] = value
    runtime, runtime_env = _arguments(spec.get("runtimeArguments"), b, way.kind == "oci")
    args, args_env = _arguments(spec.get("packageArguments"), b, way.kind == "oci")
    env |= runtime_env | args_env
    if way.kind == "npm":
        server.command, server.args = "npx", ["-y", *runtime, f"{package}@{version}" if version else package, *args]
    elif way.kind == "pypi":
        server.command, server.args = "uvx", [*runtime, f"{package}=={version}" if version else package, *args]
    else:
        if not re.search(r"(:[\w.-]+|@sha256:[0-9a-f]{64})$", package.rpartition("/")[2]) or package.endswith(":latest"):
            raise ValueError(f"Its image {package} isn't pinned to a version.")
        # Env and secrets passed by name only, never as values on docker's argv
        passed = [a for var in [*env, *b.secret_env] if f"-e {var}" not in " ".join(runtime) for a in ("-e", var)]
        server.command, server.args = "docker", ["run", "-i", "--rm", *runtime, *passed, package, *args]
    server.env, server.secret_env = env, b.secret_env
    return server, b.missing


def unlisted(name: str, label: str, source: str, package: str = "", runner: str = "", url: str = "",
             secrets: list[str] = (), settings: dict[str, str] | None = None) -> Server:
    """Unverified entry for a server found on the web (`source` is the page). A package runs via npx/uvx; a URL is
    hosted. Secrets become env vars for a package; for a URL the first is a Bearer token, the rest headers."""
    if not source.startswith(("https://", "http://")):
        raise ValueError("Say where it was found: the page's https:// link.")
    server = Server(name, label, enabled=False, source=source, verified=False, for_research=True, reviewed=False)
    settings = {k: str(v) for k, v in (settings or {}).items()}
    if url:
        server.transport, server.url = "http", url.strip()
        for i, var in enumerate(secrets):
            var = _env_var(var)
            server.secret_env[var] = _secret_name(name, var)
            header = "Authorization" if i == 0 else var.replace("_", "-").title()
            server.headers[header] = ("Bearer {%s}" % var) if header == "Authorization" else "{%s}" % var
        return server
    runner = runner or ("uvx" if "==" in package else "npx")
    if runner not in ("npx", "uvx"):
        raise ValueError("A package found on the web runs through npx (npm) or uvx (PyPI).")
    server.command = runner
    server.args = ["-y", package] if runner == "npx" else [package]
    server.env = settings
    server.secret_env = {var: _secret_name(name, var) for var in secrets}
    return server


async def _main(query: str) -> None:
    for s in await search(query):
        can, cant = ways(s)
        print(f"{s['name']}  {s.get('version', '')}\n  {label_of(s)}: {str(s.get('description', ''))[:120]}")
        for i, w in enumerate(can):
            print(f"  way {i}: {w.said()}" + (f", needs {', '.join(n.var for n in w.needs)}" if w.needs else ""))
        for w in cant:
            print(f"  can't: {w.said()}: {w.problem}")


if __name__ == "__main__":
    if not sys.argv[1:]:
        sys.exit("Usage: python -m jarvis.integrations.mcp_registry SEARCH WORDS")
    asyncio.run(_main(" ".join(sys.argv[1:])))
