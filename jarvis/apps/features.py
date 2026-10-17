"""What JARVIS can do and where each part lives, read from the code itself: docs/features.md is made from it (make
features), and a test fails when that file is stale, so the catalogue never drifts from the code.

  uv run python -m jarvis.apps.features            write docs/features.md and web/src/lib/panelActions.ts
  uv run python -m jarvis.apps.features --check    exit 1 if either is out of date

panelActions.ts lists the control panel's actions (jarvis/panel's @action functions) for the web app, so calling one
that doesn't exist fails its type check (npm run typecheck, part of make check).

Read without importing anything heavy or platform-bound: package docstrings, @tool(...) declarations in jarvis/tools,
Role.<NAME> uses, config/models.toml, the layers in tests/test_architecture.py, entry points, and the Makefile's
`## ` help lines.
"""

import ast
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "jarvis"
OUT = ROOT / "docs" / "features.md"
ACTIONS_OUT = ROOT / "web" / "src" / "lib" / "panelActions.ts"
ICONS = {"voice": "🎙️", "brain": "🧠", "gates": "🚧", "tools": "🧰", "node": "🖥️", "tasks": "⚙️", "scheduling": "⏰",
         "integrations": "🔌", "skills": "🎓", "notify": "🔔", "media": "🎞️", "projects": "📁", "state": "🗂️",
         "safety": "🛡️", "llm": "🤖", "auth": "🔐", "storage": "💾", "config": "🎛️", "prompts": "💬",
         "contracts": "📜", "core": "🧩", "apps": "🚪", "panel": "🎛️", "server": "🌐", "setup_local": "🛠️",
         "claude_login": "🔑", "bootstrap": "🔧"}


def _first_sentence(text: str | None, most: int = 220) -> str:
    """The first sentence of the first paragraph, cut at `most` characters."""
    first = " ".join((text or "").split("\n\n")[0].split())
    first = str(re.split(r":?\s+(?:uv run )?python -m ", first)[0])  # a usage line isn't what it is
    m = re.match(r"(.+?[.!?])(\s|$)", first)
    first = m.group(1) if m else first
    return (first if len(first) <= most else first[:most - 1].rsplit(" ", 1)[0] + "…").replace("|", "\\|")


def _docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def _packages() -> dict[str, Path]:
    """Top-level parts: packages (their __init__) and top-level modules."""
    found = {}
    for p in sorted(PACKAGE.iterdir()):
        if p.is_dir() and (p / "__init__.py").exists():
            found[p.name] = p / "__init__.py"
        elif p.suffix == ".py" and p.name != "__init__.py":
            found[p.stem] = p
    return found


def _files(name: str) -> list[Path]:
    base = PACKAGE / name
    return sorted(base.rglob("*.py")) if base.is_dir() else [PACKAGE / f"{name}.py"]


def _layers() -> list[tuple[str, ...]]:
    """LAYERS from tests/test_architecture.py, read as data."""
    tree = ast.parse((ROOT / "tests" / "test_architecture.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LAYERS" for t in node.targets):
            return [tuple(ast.literal_eval(e)) for e in node.value.elts]  # type: ignore[attr-defined]
    return []


def _roles_used() -> dict[str, set[str]]:
    """Role name -> the parts whose code names it (Role.TASK)."""
    used: dict[str, set[str]] = defaultdict(set)
    for name in _packages():
        for f in _files(name):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Role":
                    used[node.attr.lower()].add(name)
    return used


def _string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _tools() -> dict[str, list[tuple[str, str, list[str]]]]:
    """tools module -> [(tool name, first sentence, flags)] from each @tool(...) declaration."""
    found: dict[str, list[tuple[str, str, list[str]]]] = {}
    for f in sorted((PACKAGE / "tools").glob("*.py")):
        rows = []
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "tool" and len(d.args) >= 2:
                    flags = [k.arg for k in d.keywords if k.arg in ("gated", "reads", "changes", "shell")
                             and isinstance(k.value, ast.Constant) and k.value.value is True]
                    rows.append((_string(d.args[0]), _first_sentence(_string(d.args[1])), flags))
        if rows:
            found[f.stem] = sorted(rows)
    return found


def _entry_points() -> list[tuple[str, str]]:
    """(how it's started, what it is): jarvis.apps modules and python -m entry points."""
    found = []
    for f in sorted((PACKAGE / "apps").glob("*.py")):
        if f.name != "__init__.py":
            found.append((f"python -m jarvis.apps.{f.stem}", _first_sentence(_docstring(f))))
    for f in sorted(PACKAGE.rglob("__main__.py")):
        module = ".".join(f.relative_to(ROOT).parent.parts)
        found.append((f"python -m {module}", _first_sentence(_docstring(f))))
    return found


def _make_targets() -> list[tuple[str, str, str]]:
    """(section, target, help) from the Makefile's `## ` lines."""
    section, found = "", []
    for line in (ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^## ── (.+?) ─", line):
            section = m.group(1)
        elif m := re.match(r"^([a-z0-9-]+):.*?## (.+)$", line):
            found.append((section, m.group(1), m.group(2).replace("|", "\\|")))
    return found


def _contracts() -> list[tuple[str, str, list[str]]]:
    rows = []
    for f in sorted((PACKAGE / "contracts").glob("*.py")):
        if f.name == "__init__.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        rows.append((f.stem, _first_sentence(ast.get_docstring(tree)), names))
    return rows


def panel_actions() -> list[str]:
    """The control panel's action names: each @action function in jarvis/panel, trailing _ dropped."""
    found = set()
    for f in sorted((PACKAGE / "panel").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                    getattr(d, "id", "") == "action" for d in node.decorator_list):
                found.add(node.name.rstrip("_"))
    return sorted(found)


def render_actions() -> str:
    names = "".join(f'  "{n}",\n' for n in panel_actions())
    return ("// Generated by `make features` (jarvis/apps/features.py) from the control panel's actions in jarvis/panel.\n"
            "// Don't edit: calling an action that isn't here fails the type check.\n"
            f"export const PANEL_ACTIONS = [\n{names}] as const;\n\n"
            "export type PanelAction = (typeof PANEL_ACTIONS)[number];\n")


def render() -> str:
    packages, layers, roles_used = _packages(), _layers(), _roles_used()
    models = tomllib.loads((ROOT / "config" / "models.toml").read_text(encoding="utf-8"))
    tools, entries, targets, contracts = _tools(), _entry_points(), _make_targets(), _contracts()
    out = ['<div align="center">', "", '<img src="assets/logo.svg" width="72" alt="">', "", "# 🧭 Features", "",
           "**Every part of JARVIS, what it can do and where it lives, generated from the code.**", "",
           "[🏠 Home](../README.md) · [⚙️ Configuration](configuration.md) · [👩‍💻 Development](development.md) · "
           "[🏗️ Architecture](architecture.md) · **🧭 Features**", "", "</div>", "", "---", "",
           "> [!NOTE]",
           "> **Generated by `make features`** (`jarvis/apps/features.py`) from package docstrings, `@tool(...)` "
           "declarations, `Role` uses, `config/models.toml`, the layers in `tests/test_architecture.py`, entry "
           "points and the Makefile. Don't edit it by hand: `tests/test_features_doc.py` fails when it's stale.", "",
           f"**{len(packages)} parts** in **{len(layers)} layers**, **{sum(len(v) for v in tools.values())} tools** "
           f"in **{len(tools)} groups**, **{len(models['roles'])} model roles**, **{len(entries)} processes**.", ""]
    out += ["## 🧱 Layers", "", "Imports point down; parts in one layer don't import each other "
            "(`tests/test_architecture.py`).", "", "```mermaid", "flowchart TB"]
    for i, layer in enumerate(layers):
        out.append(f'    L{i}["{" · ".join(layer)}"]')
    out += ["    " + " --> ".join(f"L{i}" for i in range(len(layers))), "```", ""]
    out += ["## 📦 Parts", "", "| Layer | Part | What it is | Model roles it uses |", "|---:|---|---|---|"]
    for i, layer in enumerate(layers):
        for name in layer:
            if name not in packages:
                continue
            used = ", ".join(f"`{r}`" for r, who in sorted(roles_used.items()) if name in who) or ""
            kind = "/" if packages[name].name == "__init__.py" else ".py"
            out.append(f"| {i} | {ICONS.get(name, '')} `jarvis/{name}{kind}` | "
                       f"{_first_sentence(_docstring(packages[name]))} | {used} |")
    out += ["", "## 🧰 Tools the brain can call", "",
            "JARVIS's own tools, by the module that declares them. Google and Mac tools come from AI Armory "
            "(`jarvis/integrations/armory.py`) and are listed in the app. "
            "🔒 waits for a spoken yes · 👁️ only reads · ✏️ changes something · 🐚 may carry a shell command.", ""]
    marks = {"gated": "🔒", "reads": "👁️", "changes": "✏️", "shell": "🐚"}
    for module, rows in tools.items():
        out += ["<details>", f"<summary><b><code>tools/{module}.py</code></b>: {len(rows)} tools</summary>", "",
                "| Tool | | What it does |", "|---|:-:|---|"]
        out += [f"| `{n}` | {''.join(marks[f] for f in flags)} | {d} |" for n, d, flags in rows]
        out += ["", "</details>", ""]
    out += ["## 🤖 Model roles", "", "Code asks for a role; `config/models.toml` says which model plays it "
            "([Configuration](configuration.md)).", "", "| Role | Default | Used by |", "|---|---|---|"]
    for role, given in models["roles"].items():
        out.append(f"| `{role}` | {given or '*(off)*'} | "
                   f"{', '.join(f'`{p}`' for p in sorted(roles_used.get(role, ()))) or '*(config only)*'} |")
    out += ["", "## 🚪 Processes", "", "| Started with | What it is |", "|---|---|"]
    out += [f"| `{how}` | {what} |" for how, what in entries]
    out += ["", "## 📜 Contracts", "", "What a lower layer names so a higher one can fill it in "
            "(`jarvis/contracts/`), wired at start by `jarvis/bootstrap.py`.", "",
            "| Contract | What it is | Defines |", "|---|---|---|"]
    out += [f"| `contracts/{n}.py` | {d} | {', '.join(f'`{c}`' for c in names)} |" for n, d, names in contracts]
    out += ["", "## 🛠️ Make targets", "", "| Area | Target | What it does |", "|---|---|---|"]
    out += [f"| {section} | `make {t}` | {h} |" for section, t, h in targets]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    made = {OUT: render(), ACTIONS_OUT: render_actions()}
    if argv == ["--check"]:
        stale = [p for p, text in made.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        for p in stale:
            print(f"{p.relative_to(ROOT)} is out of date: run make features", file=sys.stderr)
        return 1 if stale else 0
    for p, text in made.items():
        p.write_text(text, encoding="utf-8")
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
