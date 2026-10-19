"""The long prompts, kept as markdown beside this file, one file each, and read with load().

A prompt's {{name}} placeholders are filled from load()'s keyword arguments, and each one must be given, except
{{os}} (the OS Alfred runs on, e.g. "macOS" or "Linux"), which any prompt may use; anything else in braces (JSON in a
prompt, say) is left as it is. A comment at the top of a file (<!-- ... -->) says where it's used and what fills it,
and is left out of the prompt.
"""

import re
from pathlib import Path

from alfred.core import platform

DIR = Path(__file__).parent
_SLOT = re.compile(r"\{\{(\w+)\}\}")
_NOTE = re.compile(r"\A<!--.*?-->\n*", re.DOTALL)
GIVEN = {"os": platform.NAME}  # filled in wherever a prompt uses it


def load(name: str, /, **values: object) -> str:
    """The prompt in <name>.md, its placeholders filled from `values`, without its leading comment or final newline."""
    text = _NOTE.sub("", (DIR / f"{name}.md").read_text(encoding="utf-8")).rstrip("\n")
    slots = set(_SLOT.findall(text))
    values = {k: v for k, v in GIVEN.items() if k in slots} | values
    if missing := slots - values.keys():
        raise KeyError(f"The prompt {name}.md needs {', '.join(sorted(missing))}")
    if unused := values.keys() - slots:
        raise TypeError(f"The prompt {name}.md has no place for {', '.join(sorted(unused))}")
    return _SLOT.sub(lambda m: str(values[m.group(1)]), text)
