"""What a script's own code does, for the command check (jarvis.safety.commands).

  python      read through its syntax tree: calls, imports, the commands it spawns, local modules it imports
  javascript  read through patterns (node, bun, deno): the same, more loosely

Each returns why the code needs a yes in the command check's words ('deletes files', 'sends data off the computer'),
SECRETS, UNSEEN when it runs code the check can't follow (eval of a built string, a command line made at run time, a
local module that can't be read), or ''. Errs toward asking. Libraries are trusted like any program on the PATH; local
modules are read and judged in turn. The commands a script spawns go back to the command check through `Context`.
"""

import ast
import re
import shlex
import sys
from pathlib import Path
from typing import Protocol

UNSEEN = "runs code it can't see"
SECRETS = "touches secrets"
NETWORK = "sends data off the computer"
_VALUE = "$X"  # a word known only at run time: never a literal to the command check


class Context(Protocol):
    """What the analysers ask of the command check."""

    def shell(self, line: str) -> str:
        """Why a shell line the script runs is risky, or ''."""
        ...

    def secret(self, text: str) -> bool:
        """Whether text names a secret or other processes' environments."""
        ...

    def startup(self, text: str) -> str:
        """Why writing to the path in `text` changes what runs at login or system-wide, or ''."""
        ...

    def follow(self, path: Path, language: str, roots: tuple[Path, ...] | None) -> str:
        """Why a local file the script loads is risky, UNSEEN if it can't be read, or ''."""
        ...


def first(whys: list[str]) -> str:
    """The reason to report: secrets first, then any other risk, then unseen code."""
    if SECRETS in whys:
        return SECRETS
    return next((w for w in whys if w and w != UNSEEN), "") or (UNSEEN if UNSEEN in whys else "")


# ── Python ──────────────────────────────────────────────────────

_PY_NETWORK = ("socket", "http.client", "urllib.request", "urllib3", "requests", "httpx", "aiohttp", "smtplib",
               "ftplib", "poplib", "imaplib", "nntplib", "telnetlib", "xmlrpc.client", "socketserver", "paramiko",
               "fabric", "pycurl", "websocket", "websockets", "grpc", "boto3", "botocore", "google.cloud",
               "googleapiclient", "httplib2", "mechanize", "scrapy", "selenium", "playwright", "openai", "anthropic",
               "slack_sdk", "twilio", "sendgrid", "yagmail", "dropbox", "pysftp", "asyncssh", "zmq",
               "asyncio.open_connection", "asyncio.open_unix_connection", "asyncio.start_server")
_PY_SECRETS = ("dotenv", "keyring")
# Code it can't follow: native calls, code objects, other interpreters, exec-family process replacement
_PY_UNSEEN = ("ctypes", "cffi", "code.interact", "code.InteractiveConsole", "code.InteractiveInterpreter", "marshal",
              "types.CodeType", "types.FunctionType", "importlib.util",
              "importlib.machinery", "zipimport", "runpy.run_module")
_PY_EXEC = ("os.exec", "os.spawn", "os.posix_spawn")  # each a family: os.execv, os.spawnlp...
_PY_DYNAMIC = {"eval", "exec", "compile", "execfile", "__import__", "importlib.import_module", "importlib.__import__",
               "runpy.run_path"}
_PY_DELETES = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.truncate", "shutil.rmtree",
               "send2trash.send2trash"}
_PY_PERMISSIONS = {"os.chmod", "os.lchmod", "os.fchmod", "os.chown", "os.lchown", "os.fchown", "os.chflags",
                   "os.lchflags", "shutil.chown"}
_PY_KILLS = {"os.kill", "os.killpg", "signal.pthread_kill"}
_PY_INSTALLS = ("pip.main", "pip._internal", "ensurepip")
_PY_SHELL_LINE = {"os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput",
                  "asyncio.create_subprocess_shell"}
_PY_ARGV = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen",
            "asyncio.create_subprocess_exec", "pty.spawn"}
_PY_WRITES = {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move", "os.rename",
              "os.replace", "os.symlink", "os.link"}
# Methods on objects the check can't type (Path.unlink, Path.chmod): judged by name alone
_METHOD_DELETES = {"unlink", "rmdir", "rmtree"}
_METHOD_PERMISSIONS = {"chmod", "lchmod", "chown"}
_METHOD_WRITES = {"write_text", "write_bytes", "symlink_to", "hardlink_to", "rename"}
_DUNDERS = {"__dict__", "__builtins__", "__globals__", "__subclasses__", "__code__", "__getattribute__", "__loader__",
            "__import__"}
_STAR_RISKY = {"os", "subprocess", "shutil", "importlib", "builtins", "socket", "ctypes"}


def _under(name: str, modules) -> bool:
    """Whether `name` is one of `modules` or inside one."""
    return any(name == m or name.startswith(m + ".") for m in modules)


def _text(node: ast.AST) -> str | None:
    """A string literal's value, or None if built at run time."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Python:
    def __init__(self, tree: ast.Module, path: Path | None, roots: tuple[Path, ...] | None, ctx: Context) -> None:
        self.tree, self.path, self.roots, self.ctx = tree, path, roots, ctx
        self.aliases: dict[str, str] = {}
        self.whys: list[str] = []
        self.local: list[Path] = []  # files local imports load
        self.called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def judge(self) -> str:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                    self._imported(a.name, 0)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for a in node.names:
                    if a.name == "*":
                        self._imported(module, node.level, star=True)
                        continue
                    self.aliases[a.asname or a.name] = f"{module}.{a.name}" if module and not node.level else a.name
                    self._imported(f"{module}.{a.name}" if module else a.name, node.level)
        self.whys += [self._node(node) for node in ast.walk(self.tree)]
        self.whys.append(self._startup())
        self.whys += [self.ctx.follow(f, "python", self.roots) for f in dict.fromkeys(self.local)]
        return first(self.whys)

    # ── imports ──
    def _imported(self, module: str, level: int, star: bool = False) -> None:
        """Note what an import brings in: a risky library, or local files to read."""
        if not level:
            if _under(module, _PY_NETWORK):
                self.whys.append(NETWORK)
            if _under(module, _PY_SECRETS):
                self.whys.append(SECRETS)
            if _under(module, _PY_UNSEEN):
                self.whys.append(UNSEEN)
        files = self._local(module, level)
        if files is None:
            if star and module.split(".")[0] in _STAR_RISKY:
                self.whys.append(UNSEEN)  # every name could be one of theirs
            return
        self.local += files

    def _local(self, module: str, level: int) -> list[Path] | None:
        """Files a local import loads (each package's __init__ on the way), or None for a library."""
        parts = [p for p in module.split(".") if p]
        if level:
            if self.path is None or len(self.path.parents) < level:
                return []  # inline code: Python refuses a relative import there
            roots: tuple[Path, ...] = (self.path.parents[level - 1],)
            if not parts:
                return [roots[0] / "__init__.py"] if (roots[0] / "__init__.py").is_file() else []
        elif self.roots is None:  # where it runs is unknown: only the standard library is surely not local
            if parts[:1] and parts[0] not in sys.stdlib_module_names:
                self.whys.append(UNSEEN)
            return None
        else:
            roots = self.roots
        for root in roots:
            here, files = root, []
            for i, part in enumerate(parts):
                here = here / part
                if (here / "__init__.py").is_file():
                    files.append(here / "__init__.py")
                elif here.with_suffix(".py").is_file():
                    files.append(here.with_suffix(".py"))
                    break  # a module: anything after is a name in it
                elif here.is_dir() and i:
                    continue  # namespace package inside a local one
                else:
                    break  # a name in the module before, or (first part) not in this root
            if files:
                return files
        return None if not level else []

    # ── names and calls ──
    def _name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id).removeprefix("builtins.")
        if isinstance(node, ast.Attribute) and (base := self._name(node.value)):
            return f"{base}.{node.attr}".removeprefix("builtins.")
        return ""

    def _node(self, node: ast.AST) -> str:
        if isinstance(node, ast.Attribute) and node.attr in _DUNDERS:
            return UNSEEN
        if (text := _text(node)) is not None:
            return SECRETS if self.ctx.secret(text) else ""
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in self.called:
            name = self._name(node)
            if _under(name, _PY_NETWORK):
                return NETWORK
            if name in _PY_DELETES:
                return "deletes files"  # handed on, as in map(os.remove, files)
            if (name in _PY_SHELL_LINE | _PY_ARGV | _PY_DYNAMIC or _under(name, _PY_UNSEEN) or name.startswith(_PY_EXEC)
                    or name == "getattr"):
                return UNSEEN
        return ""

    def _call(self, call: ast.Call) -> str:
        name = self._name(call.func)
        method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        if name in _PY_DELETES or method in _METHOD_DELETES:
            return "deletes files"
        if name in _PY_PERMISSIONS or method in _METHOD_PERMISSIONS:
            return "changes permissions"
        if name in _PY_KILLS:
            return "stops a process"
        if _under(name, _PY_INSTALLS):
            return "installs software"
        if _under(name, _PY_UNSEEN) or name.startswith(_PY_EXEC):
            return UNSEEN
        if name in _PY_DYNAMIC:
            return self._dynamic(name, call)
        if name == "getattr" and (len(call.args) < 2 or _text(call.args[1]) is None):
            return UNSEEN  # getattr(os, name): any function at all
        if name in _PY_SHELL_LINE:
            return self._spawn(call, shell=True)
        if name in _PY_ARGV:
            shell = any(k.arg == "shell" and not (isinstance(k.value, ast.Constant) and not k.value.value)
                        for k in call.keywords)
            return self._spawn(call, shell=shell)
        return ""

    def _dynamic(self, name: str, call: ast.Call) -> str:
        """eval/exec/import of a literal is judged as that code or import; of anything built at run time, unseen."""
        source = _text(call.args[0]) if call.args else None
        if source is None:
            return UNSEEN
        if name in ("eval", "exec", "compile", "execfile"):
            return python(source, None, self.roots, self.ctx) if name != "execfile" else UNSEEN
        if name == "runpy.run_path":
            path = Path(source).expanduser()
            return self.ctx.follow(path, "python", self.roots) if path.is_absolute() else UNSEEN
        self._imported(source, 0)
        return ""

    def _spawn(self, call: ast.Call, shell: bool) -> str:
        """A spawned command, rebuilt as a shell line from its literal parts, goes back to the command check."""
        args = call.args or [k.value for k in call.keywords if k.arg in ("args", "cmd")]
        if not args or any(k.arg == "executable" for k in call.keywords):
            return UNSEEN
        if self._name(call.func) == "asyncio.create_subprocess_exec":
            words = [self._word(a) for a in args]
        elif isinstance(args[0], (ast.List, ast.Tuple)):
            if shell:  # shell=True with a list: the first item is the line
                line = _text(args[0].elts[0]) if args[0].elts else None
                return UNSEEN if line is None else self.ctx.shell(self._where(call) + line)
            words = [self._word(e) for e in args[0].elts]
        elif (line := _text(args[0])) is not None:
            if shell:
                return self.ctx.shell(self._where(call) + line)
            words = [shlex.quote(line)]
        else:
            return UNSEEN  # a command line built at run time
        if not words or words[0] == _VALUE:
            return UNSEEN
        return self.ctx.shell(self._where(call) + " ".join(words))

    def _where(self, call: ast.Call) -> str:
        """'cd DIR && ' for the cwd= a spawned command runs in."""
        cwd = next((k.value for k in call.keywords if k.arg == "cwd"), None)
        if cwd is None:
            return ""
        return f"cd {_VALUE if (text := _text(cwd)) is None else shlex.quote(text)} && "

    def _word(self, node: ast.AST) -> str:
        if (text := _text(node)) is not None:
            return shlex.quote(text)
        return "python" if self._name(node) == "sys.executable" else _VALUE

    def _startup(self) -> str:
        """A login item, unit or /etc path named in a script that writes files: what runs at login may change."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                name, method = self._name(node.func), node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if name in _PY_WRITES or method in _METHOD_WRITES or (name == "open" or method == "open") and _writes_mode(
                        node, 1 if name == "open" else 0):
                    texts = (t for n in ast.walk(self.tree) if (t := _text(n)) is not None)
                    return next((why for t in texts if (why := self.ctx.startup(t))), "")
        return ""


def _writes_mode(call: ast.Call, at: int) -> bool:
    """Whether open(path, mode) (`at` 1) or Path.open(mode) (`at` 0) opens for writing."""
    mode = call.args[at] if len(call.args) > at else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False
    text = _text(mode)
    return text is None or bool(set(text) & set("wax+"))


def python(source: str, path: Path | None, roots: tuple[Path, ...] | None, ctx: Context) -> str:
    """Why Python source needs a yes. `path`: its file, for relative imports; `roots`: where its local imports are
    found (the script's folder, or the working folder for -c and stdin)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return UNSEEN  # not Python the check can read
    return _Python(tree, path, roots, ctx).judge()


# ── JavaScript ──────────────────────────────────────────────────

_JS_QUOTED = r"""(?:'((?:\\.|[^'\\\n])*)'|"((?:\\.|[^"\\\n])*)"|`((?:\\.|[^`\\$])*)`)"""
_JS_LOADS = re.compile(rf"""\b(?:require|import)\s*\(\s*{_JS_QUOTED}\s*\)|\bimport\s+(?:[\w$*{{}}\s,]+?\s+from\s+)?"""
                       rf"""{_JS_QUOTED}|\bexport\s+[\w$*{{}}\s,]+?\s+from\s+{_JS_QUOTED}""")
_JS_LOAD_BUILT = re.compile(rf"""\b(?:require|import)\s*\((?!\s*{_JS_QUOTED}\s*\))""")
_JS_STRINGS = re.compile(_JS_QUOTED)
_JS_NETWORK = {"http", "https", "net", "tls", "dgram", "http2", "axios", "node-fetch", "undici", "got", "request",
               "superagent", "ws", "socket.io-client", "nodemailer", "ssh2", "ftp", "basic-ftp", "puppeteer",
               "playwright", "aws-sdk", "openai", "@anthropic-ai/sdk", "@aws-sdk", "@slack/web-api", "twilio"}
_JS_SPAWNS = {"child_process", "cross-spawn"}
_JS_UNSEEN = {"vm", "module", "worker_threads", "execa", "shelljs", "zx", "ffi-napi", "node-ffi"}
_JS_SECRETS = {"dotenv", "keytar"}
_JS_DELETERS = {"rimraf", "del", "trash"}
_JS_NETWORK_CALLS = re.compile(r"(?<![\w$.])fetch\s*\(|\bnew\s+(?:WebSocket|EventSource|XMLHttpRequest)\b"
                               r"|\bnavigator\.sendBeacon\b")
# Code built at run time or hidden: eval and friends, computed globals, letters spelled as escapes or char codes
_JS_DYNAMIC = re.compile(
    r"(?<![\w$.])eval\s*\(|(?<![\w$.])(?:new\s+)?Function\s*\(|\bset(?:Timeout|Interval|Immediate)\s*\(\s*['\"`]"
    r"|\bprocess\.(?:binding|dlopen|mainModule)\b|\bWebAssembly\.|\bconstructor\s*\.\s*constructor|\bnew\s+Worker\s*\("
    r"|\bglobal(?:This)?\s*\[|\bString\.fromCharCode\b|(?<![\w$.])atob\s*\(|\[\s*['\"`][^'\"`\n]*['\"`]\s*\+"
    r"|\\x(?:[46][0-9a-fA-F]|[57][0-9aA])|\\u00(?:[46][0-9a-fA-F]|[57][0-9aA])|\\u\{")
_JS_DELETE_CALLS = re.compile(r"(?<![\w$])(?:rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync|truncate|truncateSync"
                              r"|ftruncate|ftruncateSync)\s*\(")
_JS_EXTRA_DELETES = re.compile(r"(?<![\w$])(?:remove|removeSync|emptyDir|emptyDirSync)\s*\(")  # fs-extra
_JS_PERMISSION_CALLS = re.compile(r"(?<![\w$])(?:l?chmod|l?chown|fchmod|fchown)(?:Sync)?\s*\(")
_JS_KILLS = re.compile(r"\bprocess\.kill\s*\(")
_JS_WRITES = re.compile(r"(?<![\w$])(?:writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream|copyFile"
                        r"|copyFileSync|cpSync|rename|renameSync|symlink|symlinkSync|outputFile|writeJson)\s*\(")
_JS_SPAWN_CALL = re.compile(r"(?<![\w$])(exec|execSync|execFile|execFileSync|spawn|spawnSync|fork)\s*\(")
JS_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")


def _js_string(match: re.Match) -> str:
    """A matched JS string literal's value (simple escapes only: letters spelled as escapes count as unseen)."""
    raw = next(g for g in match.groups() if g is not None)
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), raw)


def _js_module(spec: str) -> str:
    """The package a bare specifier names: 'fs' for 'node:fs/promises', '@aws-sdk/x' for '@aws-sdk/x/y'."""
    parts = spec.removeprefix("node:").split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _js_file(spec: str, base: Path | None) -> Path | None:
    """The local file a relative or absolute specifier loads, or None if it isn't there."""
    path = Path(spec).expanduser()
    if not path.is_absolute():
        if base is None:
            return None
        path = base / path
    candidates = [path, *(path.with_name(path.name + e) for e in JS_EXTENSIONS),
                  *(path / f"index{e}" for e in JS_EXTENSIONS)]
    return next((c for c in candidates if c.is_file()), None)


def _js_spawns(source: str, ctx: Context) -> list[str]:
    """Each child_process call, rebuilt as a shell line from its literal arguments, judged by the command check."""
    if re.search(r"\bshell\s*:\s*(?!false\b)", source):
        return [UNSEEN]  # spawn(..., {shell: true}) joins its words into a line
    whys = []
    for m in _JS_SPAWN_CALL.finditer(source):
        rest = source[m.end():]
        if not (head := re.match(rf"\s*{_JS_QUOTED}", rest)):
            whys.append(UNSEEN)  # a command made at run time
            continue
        value, after = _js_string(head), rest[head.end():]
        if m.group(1) in ("exec", "execSync"):
            whys.append(ctx.shell(value))
            continue
        words = ["node" if m.group(1) == "fork" else "", shlex.quote(value)]
        if array := re.match(r"\s*,\s*\[([^\]]*)\]", after):
            for item in (i.strip() for i in array.group(1).split(",") if i.strip()):
                literal = re.fullmatch(_JS_QUOTED, item)
                words.append(shlex.quote(_js_string(literal)) if literal else _VALUE)
        elif re.match(r"\s*,\s*[^\s{]", after):
            words.append(_VALUE)  # arguments built at run time
        whys.append(ctx.shell(" ".join(w for w in words if w)))
    return whys


def javascript(source: str, path: Path | None, cwd: Path | None, ctx: Context) -> str:
    """Why JavaScript (or TypeScript) source needs a yes. Relative imports load from `path`'s folder, or `cwd` for
    inline code."""
    if _JS_LOAD_BUILT.search(source) or _JS_DYNAMIC.search(source):
        return UNSEEN
    if any(ctx.secret(_js_string(m)) for m in _JS_STRINGS.finditer(source)):
        return SECRETS
    whys, modules = [], set()
    base = path.parent if path else cwd
    for m in _JS_LOADS.finditer(source):
        spec = _js_string(m)
        if not spec.startswith((".", "/", "~")):
            modules.add(_js_module(spec))
        elif not spec.endswith(".json"):  # .json is data
            file = _js_file(spec, base)
            whys.append(UNSEEN if file is None or file.suffix == ".node" else ctx.follow(file, "javascript", ()))
    if modules & _JS_SECRETS:
        return SECRETS
    if modules & _JS_UNSEEN:
        whys.append(UNSEEN)
    if modules & _JS_NETWORK or _JS_NETWORK_CALLS.search(source):
        whys.append(NETWORK)
    if (modules & _JS_DELETERS or _JS_DELETE_CALLS.search(source)
            or "fs-extra" in modules and _JS_EXTRA_DELETES.search(source)):
        whys.append("deletes files")
    if _JS_PERMISSION_CALLS.search(source):
        whys.append("changes permissions")
    if _JS_KILLS.search(source):
        whys.append("stops a process")
    if modules & _JS_SPAWNS:
        whys += _js_spawns(source, ctx)
    if _JS_WRITES.search(source):
        whys += [ctx.startup(_js_string(m)) for m in _JS_STRINGS.finditer(source)]
    return first(whys)
