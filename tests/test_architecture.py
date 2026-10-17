"""The package layers (docs/architecture-review.md): imports point down, siblings in one layer don't import each other,
only jarvis.llm talks to the Claude SDK, and no model id is written in the code.

Imports that break the rules today are listed in architecture_baseline.txt. The list only shrinks: a new break fails,
and so does a listed one that is gone, so whoever fixes it also takes it off the list. To see the current breaks:
`uv run python tests/test_architecture.py`.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "jarvis"
BASELINE = Path(__file__).with_name("architecture_baseline.txt")

# Top first. A module may import its own package and any package in a lower layer; the entry points, which nothing
# imports, may also use each other.
LAYERS = (
    ("apps", "server", "panel", "setup_local", "claude_login"),  # entry points
    ("bootstrap",),  # wires everything below
    ("voice",),  # the call: hears, speaks, takes turns; drives the brain
    ("brain",),  # the voice brain and its lookups; uses the tools
    ("gates",),  # what every tool call passes: spoken yes for risky ones, screening of results
    ("tools",),  # what the brain can call: thin adapters over the layers below
    ("node",),  # the user's other machines: sending them work (jarvis.contracts.remote), and running it there
    ("tasks", "scheduling"),  # orchestration: running work
    ("integrations", "skills", "notify", "media", "projects"),  # services the orchestration and tools use
    ("state",),  # what JARVIS keeps for the user: memory, to-dos, settings, history
    ("safety",),  # policy: what may run, be read or be said; may ask a model to judge
    ("llm",),  # the model provider: the only code that talks to Claude
    ("auth",),  # secrets: the vault, JARVIS's own keys, panel logins
    ("storage",),  # the database and the active profile
    ("config", "prompts"),
    ("contracts",),
    ("core",),
)
LAYER = {name: depth for depth, names in enumerate(LAYERS) for name in names}
# Sibling imports that are allowed by design: (importer, imported)
SIBLINGS = {
    ("media", "integrations"),  # documents read HTML the way gmail_read does, through the AI Armory facade
    ("notify", "integrations"),  # the Web Push contact falls back to the default Google account's address
}

# Only these may import these third-party packages
OWNERS = {"claude_agent_sdk": "jarvis.llm", "ai_armory": "jarvis.integrations.armory"}
# A Claude model id written in code; ids come from config/models.toml
MODEL_ID = re.compile(r"""["']claude-(?:opus|sonnet|haiku|fable)[\w.-]*["']""")


def package_of(module: str) -> str:
    """'jarvis.tasks.runner' -> 'tasks'; the jarvis package itself sits with core (it only checks the OS)."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "core"


def modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts), path


def _main_block(node: ast.AST) -> bool:
    """`if __name__ == "__main__":`, whose imports run only when the module is started as a program."""
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__")


def _nodes(tree: ast.AST):
    """Every node, leaving out what's under `if __name__ == "__main__":`."""
    for child in ast.iter_child_nodes(tree):
        if not _main_block(child):
            yield child
            yield from _nodes(child)


def imports(module: str, path: Path):
    """(imported module, line) for every import in the file, relative ones resolved. Those a module makes only when
    started as a program (`if __name__ == "__main__":`) are an entry point's and left out."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in _nodes(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")[: len(package.split(".")) - node.level + 1] if node.level else []
            target = ".".join(base + ([node.module] if node.module else []))
            if target == "jarvis":  # `from jarvis import tasks` imports the package tasks
                for alias in node.names:
                    yield f"jarvis.{alias.name}", node.lineno
            else:
                yield target, node.lineno


def layer_breaks() -> set[str]:
    """'importer -> package' for every import that points up, or sideways to a sibling."""
    found = set()
    for module, path in modules():
        mine = "apps" if module.endswith(".__main__") else package_of(module)  # `python -m` entry points
        for target, _ in imports(module, path):
            if target.split(".")[0] != "jarvis":
                continue
            theirs = package_of(target)
            if theirs == mine or mine not in LAYER or theirs not in LAYER:
                continue
            if LAYER[theirs] < LAYER[mine] or (LAYER[theirs] == LAYER[mine] != 0 and (mine, theirs) not in SIBLINGS):
                found.add(f"{module} -> {theirs}")
    return found


def owner_breaks() -> set[str]:
    found = set()
    for module, path in modules():
        for target, _ in imports(module, path):
            owner = OWNERS.get(target.split(".")[0])
            if owner and not (module == owner or module.startswith(owner + ".")):
                found.add(f"{module} -> {target.split('.')[0]}")
    return found


def model_ids() -> set[str]:
    return {f"{module} -> model id" for module, path in modules() if MODEL_ID.search(path.read_text(encoding="utf-8"))}


def current() -> set[str]:
    return layer_breaks() | owner_breaks() | model_ids()


def baseline() -> set[str]:
    lines = BASELINE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def test_every_package_has_a_layer():
    packages = {package_of(module) for module, _ in modules()}
    assert packages <= LAYER.keys(), f"Give these a layer in LAYERS: {sorted(packages - LAYER.keys())}"


def test_no_new_import_breaks_the_layers():
    new = current() - baseline()
    assert not new, ("These imports break the layers (docs/architecture-review.md). Use a contract from "
                     "jarvis.contracts, or move the code:\n  " + "\n  ".join(sorted(new)))


def test_the_baseline_only_lists_breaks_that_still_exist():
    gone = baseline() - current()
    assert not gone, "Fixed, so take these off tests/architecture_baseline.txt:\n  " + "\n  ".join(sorted(gone))


if __name__ == "__main__":
    breaks = sorted(current())
    print("\n".join(breaks))
    print(f"{len(breaks)} breaks", file=sys.stderr)
