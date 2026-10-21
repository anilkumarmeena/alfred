"""What a script's own code does, for the command check (alfred.safety.commands).

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

from alfred.safety.risk import Risk

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
        """Whether text names a secret or other processes' environments (its own project's env files aside: those are
        noted, and it may read them)."""
        ...

    def keys(self) -> bool:
        """Whether the script may load its project's env files with dotenv, noting that it does; if not, they're
        secrets."""
        ...

    def env_file(self, text: str) -> bool:
        """Whether text names an env file: one the script may read, if its project's, but never copy."""
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
_PY_SECRETS = ("keyring",)
_PY_DOTENV = ("dotenv",)  # loads the project's env files: allowed in a project (Context.keys)
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
# Paths built in code, followed so an env file named in pieces is judged as the whole path
_PY_JOINS = {"os.path.join", "posixpath.join", "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath"}
_PY_SAME_PATH = {"os.path.abspath", "os.path.realpath", "os.path.normpath", "str", "os.fspath"}
_PY_CWD = {"os.getcwd", "pathlib.Path.cwd"}


def _under(name: str, modules) -> bool:
    """Whether `name` is one of `modules` or inside one."""
    return any(name == m or name.startswith(m + ".") for m in modules)


def _up(path: Path | None) -> Path | None:
    """The folder holding `path` (relative ones: where the script runs), or None if unknown."""
    if path is None:
        return None
    return path.parent if path.is_absolute() or path.parts and path.parts[-1] != ".." else path / ".."


def _joined(parts: list[Path | None]) -> Path | None:
    """The parts joined as os.path.join does (an absolute one starts again), or None if one is unknown."""
    known = [p for p in parts if p is not None]
    return Path(*known) if len(known) == len(parts) else None


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
        self.set: dict[str, list[ast.AST]] = {}  # names set at the top level, to what
        for stmt in tree.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    if isinstance(target, ast.Name):
                        self.set.setdefault(target.id, []).append(stmt.value)
        self.built: list[ast.AST] = []  # paths joined from pieces, judged whole (_path)
        self.in_built: set[int] = set()

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
        self.built = [node for node in ast.walk(self.tree) if self._builds(node)]
        self.in_built = {id(n) for node in self.built for n in ast.walk(node)}
        self.whys += [self._node(node) for node in ast.walk(self.tree)]
        self.whys += [self._path(node) for node in self.built]
        self.whys.append(self._startup())
        self.whys += [self.ctx.follow(f, "python", self.roots) for f in dict.fromkeys(self.local)]
        return first(self.whys)

    # ── imports ──
    def _imported(self, module: str, level: int, star: bool = False) -> None:
        """Note what an import brings in: a risky library, or local files to read."""
        if not level:
            if _under(module, _PY_NETWORK):
                self.whys.append(NETWORK)
            if _under(module, _PY_SECRETS) or (_under(module, _PY_DOTENV) and not self.ctx.keys()):
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
            if id(node) in self.in_built and self.ctx.env_file(text):
                return ""  # a piece of a path: the whole is judged (_path)
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
            return Risk.PERMISSIONS
        if name in _PY_KILLS:
            return Risk.STOPS
        if _under(name, _PY_INSTALLS):
            return Risk.INSTALLS
        if _under(name, _PY_UNSEEN) or name.startswith(_PY_EXEC):
            return UNSEEN
        if name in _PY_DYNAMIC:
            return self._dynamic(name, call)
        if name == "getattr" and (len(call.args) < 2 or _text(call.args[1]) is None):
            return UNSEEN  # getattr(os, name): any function at all
        if name in _PY_WRITES and any(self.ctx.env_file(t) for a in call.args if (t := _text(a)) is not None):
            return SECRETS  # shutil.copy('.env', ...): a copy of its keys
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

    # ── paths joined from pieces ──
    def _builds(self, node: ast.AST) -> bool:
        """Whether `node` joins a path from pieces: os.path.join, Path(a, b), joinpath, / or +, an f-string."""
        if isinstance(node, ast.BinOp):
            return isinstance(node.op, (ast.Div, ast.Add))
        if isinstance(node, ast.Call):
            name = self._name(node.func)
            return (name in ("os.path.join", "posixpath.join") or name in _PY_JOINS and len(node.args) > 1
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath")
        return isinstance(node, ast.JoinedStr)

    def _path(self, node: ast.AST) -> str:
        """SECRETS if a joined path is an env file the script mayn't read, or has an env file's name in it and can't
        be followed (os.path.join(ALFRED, '.env'), ALFRED set to a folder the check can't tell)."""
        if (value := self._value(node)) is None:
            return SECRETS if any(self.ctx.env_file(t) for n in ast.walk(node) if (t := _text(n)) is not None) else ""
        return SECRETS if self.ctx.env_file(str(value)) and self.ctx.secret(str(value)) else ""

    def _value(self, node: ast.AST, depth: int = 0) -> Path | None:
        """The path an expression makes (relative: to where the script runs), or None if the check can't tell. A name
        the script doesn't set at the top level (an import, a parameter) counts as where it runs, as a bare '.env'
        does."""
        if depth > 20:
            return None
        if (text := _text(node)) is not None:
            return Path(text).expanduser()
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.path
            values = self.set.get(node.id)
            if values is None:
                return Path()
            return self._value(values[0], depth + 1) if len(values) == 1 else None
        if isinstance(node, ast.JoinedStr):
            parts = [self._value(p.value, depth + 1) if isinstance(p, ast.FormattedValue) else _text(p) for p in node.values]
            return None if None in parts else Path("".join(map(str, parts)))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left, right = self._value(node.left, depth + 1), self._value(node.right, depth + 1)
            if left is None or right is None:
                return None
            return left / right if isinstance(node.op, ast.Div) else Path(str(left) + str(right))
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            return _up(self._value(node.value, depth + 1))
        if not isinstance(node, ast.Call) or node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            return None
        name, args = self._name(node.func), list(node.args)
        on, method = (node.func.value, node.func.attr) if isinstance(node.func, ast.Attribute) else (None, "")
        if name in _PY_CWD and not args:
            return Path()
        if on is not None and method in ("resolve", "absolute") and not args:
            return self._value(on, depth + 1)
        if name == "os.path.dirname" and len(args) == 1:
            return _up(self._value(args[0], depth + 1))
        if name in _PY_SAME_PATH and len(args) == 1:
            return self._value(args[0], depth + 1)
        if on is not None and method == "joinpath":
            args.insert(0, on)
        elif name not in _PY_JOINS:
            return None
        return _joined([self._value(a, depth + 1) for a in args])

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
_JS_SECRETS = {"keytar"}
_JS_DOTENV = {"dotenv", "dotenv-expand", "@dotenvx/dotenvx"}  # loads the project's env files: allowed in a project
_JS_COPIES = re.compile(rf"(?<![\w$])(?:copyFile|copyFileSync|cp|cpSync|rename|renameSync|link|linkSync|symlink"
                        rf"|symlinkSync|copy|copySync|move|moveSync)\s*\(\s*{_JS_QUOTED}")
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


_JS_JOIN_CALL = re.compile(r"(?<![\w$.])(?:path\s*\.\s*)?(?:join|resolve)\s*\(")
_JS_PATH_CALL = re.compile(r"(?:path\s*\.\s*)?(dirname|join|resolve|normalize)\s*\(")
_JS_TEMPLATE = re.compile(r"`((?:\\.|[^`\\])*?\$\{(?:\\.|[^`\\])*)`")
_JS_FILE_URL = re.compile(r"(?:url\s*\.\s*)?fileURLToPath\s*\(\s*import\.meta\.url\s*\)|import\.meta\.filename")


def _js_args(source: str, start: int) -> tuple[list[str], int] | None:
    """The arguments of a call whose '(' is just before `start`, and the index after its ')'; None if unclosed."""
    args, depth, piece, i = [], 0, start, start
    while i < len(source):
        if string := _JS_STRINGS.match(source, i):
            i = string.end()
            continue
        if (c := source[i]) in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return ([a.strip() for a in [*args, source[piece:i]] if a.strip()], i + 1) if c == ")" else None
            depth -= 1
        elif c == "," and depth == 0:
            args.append(source[piece:i])
            piece = i + 1
        i += 1
    return None


def _js_value(expr: str, source: str, path: Path | None, depth: int = 0) -> Path | None:
    """The path a JS expression makes (relative: to where the script runs), or None if the check can't tell. A name
    the script doesn't set (an import, a parameter) counts as where it runs, as a bare '.env' does."""
    expr = expr.strip()
    if depth > 20:
        return None
    if literal := re.fullmatch(_JS_QUOTED, expr):
        return Path(_js_string(literal)).expanduser()
    if template := _JS_TEMPLATE.fullmatch(expr):
        parts = re.split(r"\$\{([^}]*)\}", template.group(1))  # text, then each ${...} and the text after it
        values = [p if i % 2 == 0 else _js_value(p, source, path, depth + 1) for i, p in enumerate(parts)]
        return None if None in values else Path("".join(map(str, values)))
    if expr == "process.cwd()":
        return Path()
    if _JS_FILE_URL.fullmatch(expr):
        return path
    if expr == "import.meta.dirname":
        return _up(path)
    if (call := _JS_PATH_CALL.match(expr)) and (found := _js_args(expr, call.end())) and found[1] == len(expr):
        values = [_js_value(a, source, path, depth + 1) for a in found[0]]
        if call.group(1) == "dirname":
            return _up(values[0]) if len(values) == 1 else None
        return _joined(values) if values else None
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        return None
    set_to = re.findall(rf"(?<![\w$.])(?:const|let|var)\s+{re.escape(expr)}\s*(?::[^=\n]+)?=\s*([^;\n]+)", source)
    if len(set_to) == 1:
        return _js_value(set_to[0], source, path, depth + 1)
    if set_to:
        return None
    return {"__filename": path, "__dirname": _up(path)}.get(expr, Path())


def _js_paths(source: str, path: Path | None, ctx: Context) -> tuple[bool, list[tuple[int, int]]]:
    """Whether a path the code joins (path.join, path.resolve, a template) is an env file it mayn't read, or has an env
    file's name in it and can't be followed; and where those joins are, whose pieces are judged whole here."""
    spans = [(m.start(), found[1]) for m in _JS_JOIN_CALL.finditer(source) if (found := _js_args(source, m.end()))]
    spans += [m.span() for m in _JS_TEMPLATE.finditer(source)]
    for a, b in spans:
        whole = source[a:b]
        named = any(ctx.env_file(_js_string(s)) for s in _JS_STRINGS.finditer(whole)) or (
            whole.startswith("`") and ctx.env_file(whole.strip("`")))
        value = _js_value(whole, source, path)
        if value is None and named or value is not None and ctx.env_file(str(value)) and ctx.secret(str(value)):
            return True, spans
    return False, spans


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
    joined, spans = _js_paths(source, path, ctx)
    if joined or any(ctx.secret(value) for m in _JS_STRINGS.finditer(source)
                     if not (ctx.env_file(value := _js_string(m)) and any(a <= m.start() < b for a, b in spans))):
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
    if modules & _JS_SECRETS or (modules & _JS_DOTENV and not ctx.keys()):
        return SECRETS
    if any(ctx.env_file(_js_string(m)) for m in _JS_COPIES.finditer(source)):
        return SECRETS  # copyFile('.env', ...): a copy of its keys
    if modules & _JS_UNSEEN:
        whys.append(UNSEEN)
    if modules & _JS_NETWORK or _JS_NETWORK_CALLS.search(source):
        whys.append(NETWORK)
    if (modules & _JS_DELETERS or _JS_DELETE_CALLS.search(source)
            or "fs-extra" in modules and _JS_EXTRA_DELETES.search(source)):
        whys.append("deletes files")
    if _JS_PERMISSION_CALLS.search(source):
        whys.append(Risk.PERMISSIONS)
    if _JS_KILLS.search(source):
        whys.append(Risk.STOPS)
    if modules & _JS_SPAWNS:
        whys += _js_spawns(source, ctx)
    if _JS_WRITES.search(source):
        whys += [ctx.startup(_js_string(m)) for m in _JS_STRINGS.finditer(source)]
    return first(whys)
