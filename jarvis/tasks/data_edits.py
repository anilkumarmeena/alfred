"""Edits to JARVIS's data that sandboxed tasks suggest (propose_data_edit) and the voice brain applies (task_edits).

Edits are validated by `check`, stored numbered on the task's `edits` column, and applied by `apply`.
  ACTIONS  mirror voice tools: project_register/update, todo_add/done/reword, remember, forget; `edit` replaces one
           exact span of memory.md within its size limits
  LEARNED  learning review only: `skill` (always needs a yes), `vocabulary` (auto-applied), `follow_up` (needs a yes)
Every text field is scanned for hidden instructions; anything suspicious is `flagged`, which forces needs_yes.
"""

from datetime import datetime
from pathlib import Path

from jarvis.core import profile
from jarvis.core.database import edits
from jarvis.core.files import write_atomic
from jarvis.safety import scan
from jarvis.skills import home as skills
from jarvis.state import memory, todo, vocabulary
from jarvis.tasks import projects, schedules

MAX_EDITS = 20  # per task
MAX_TEXT = 4000  # characters in any one field

# action -> (required fields, optional fields)
ACTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "project_register": (("name",), ("path", "aliases", "about", "in_place")),
    "project_update": (("name",), ("path", "aliases", "about", "in_place")),
    "todo_add": (("item",), ()),
    "todo_done": (("text",), ()),
    "todo_reword": (("text", "item"), ()),
    "remember": (("fact", "section"), ()),
    "forget": (("text",), ()),
    "edit": (("file", "old", "new"), ()),
}
# Only tasks of kind "review" (the learning review) may suggest these
LEARNED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "skill": (("name", "description", "instructions"), ()),
    "vocabulary": (("word",), ("heard_as",)),
    "follow_up": (("text", "when"), ()),
}
LISTS = ("aliases", "heard_as")  # list-of-text fields
FILES = ("memory.md",)
PENDING, APPLIED, DISMISSED, FAILED = "pending", "applied", "dismissed", "failed"


def _file(name: str) -> Path:
    """Path of data file `name` for the current profile."""
    if name not in FILES:  # stored edit naming a file no longer editable
        raise ValueError(f"{name} isn't a data file; the only one is {', '.join(FILES)}.")
    return {"memory.md": profile.current().memory_file}[name]


def check(args: dict, kind: str = "") -> dict:
    """Validate and normalise a suggested edit against current data; ValueError says what to fix."""
    action = args.get("action")
    if not (spec := ACTIONS.get(action) or (LEARNED.get(action) if kind == "review" else None)):
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}.")
    required, optional = spec
    reason = " ".join(str(args.get("reason") or "").split())
    if not reason:
        raise ValueError("Give a one-line reason for the edit.")
    edit = {"action": action}
    for key in (*required, *optional):
        value = args.get(key)
        blank = value == [] or (isinstance(value, str) and not value.strip() and key not in ("old", "new"))
        if value is None or blank:  # empty `new` is allowed: it deletes `old`
            if key in required:
                raise ValueError(f"{action} needs {key}.")
            continue
        if key == "in_place" and not isinstance(value, bool):
            raise ValueError("in_place must be true or false.")
        if key in LISTS and not (isinstance(value, list) and all(isinstance(a, str) for a in value)):
            raise ValueError(f"{key} must be a list of text.")
        if key not in ("in_place", *LISTS) and not isinstance(value, str):
            raise ValueError(f"{key} must be text.")
        if key == "section" and (value := str(value).strip().lower()) not in memory.SECTIONS:  # already checked to be text
            raise ValueError("section must be user or environment.")
        if len(str(value)) > (most := skills.INSTRUCTIONS_MAX if key == "instructions" else MAX_TEXT):
            raise ValueError(f"{key} is too long; keep each edit under {most} characters.")
        edit[key] = value
    extra = sorted(k for k, v in args.items() if k not in (*required, *optional, "action", "reason") and v not in (None, "", []))
    if extra:
        raise ValueError(f"{action} doesn't take {', '.join(extra)}.")
    flagged = _scanned(edit)  # flagged edits always need the user's yes
    said = scan.scan(reason)
    reason = said.clean
    if said.suspicious:
        flagged.append(f"its reason {said.reason()}")
    if flagged:
        edit["flagged"] = "; ".join(flagged)[:600]
    if action == "edit":
        if edit["file"] not in FILES:
            raise ValueError(f"file must be one of {', '.join(FILES)}; projects change with project_update.")
        _replaced(edit["file"], edit["old"], edit["new"])
    elif action == "remember":
        if not (fact := memory.plain(edit["fact"])):
            raise ValueError("The fact is empty.")
        if len(line := f"- {fact} (YYYY-MM-DD)") > memory.FACT_MAX:
            raise ValueError(f"A memory fact may be at most {memory.FACT_MAX} characters, its date included, and this "
                             f"one is {len(line)}; say it more briefly or split it.")
    elif action == "project_register":
        try:
            known = projects.find(edit["name"])
        except ValueError:
            pass
        else:
            raise ValueError(f"'{edit['name']}' is already the project {known.name}; suggest a project_update instead.")
    elif action == "skill":
        edit["name"] = skills.as_name(edit["name"])
        skills.check_text(edit["name"], edit["description"], edit["instructions"])
        edit["description"] = skills.one_line(edit["description"])
    elif action == "vocabulary":
        edit["word"], heard = vocabulary.learnable(edit["word"], edit.pop("heard_as", []))
        if heard:
            edit["heard_as"] = heard
    elif action == "follow_up":
        if schedules.parse_time(edit["when"]) <= datetime.now(profile.tz()):
            raise ValueError("A follow-up's time must be still to come.")
    elif action == "project_update":
        if edit.keys() == {"action", "name"}:
            raise ValueError("project_update needs at least one of path, aliases, about or in_place to change.")
        edit["name"] = projects.find(edit["name"]).name
    return edit | {"reason": reason[:300]}


def _scanned(edit: dict) -> list[str]:
    """Strip invisible chars from text fields in place (not `old`); return per-field hidden-instruction findings.

    Skills are meant to instruct, so they're scanned with the laxer INSTRUCTIONS source."""
    flagged = []
    source = scan.INSTRUCTIONS if edit["action"] == "skill" else scan.DATA
    for key, value in edit.items():
        values = value if key in LISTS else [value]
        if key in ("action", "old", "section", "in_place") or not all(isinstance(v, str) for v in values):
            continue
        found = [scan.scan(v, source) for v in values]
        edit[key] = [f.clean for f in found] if key in LISTS else found[0].clean
        flagged += [f"its {key} {f.reason()}" for f in found if f.suspicious]
    return flagged


def _replaced(name: str, old: str, new: str) -> str:
    """File text with the single occurrence of `old` replaced; ValueError if not unique or memory limits break."""
    path = _file(name)
    text = path.read_text() if path.exists() else ""
    if (count := text.count(old)) != 1:
        raise ValueError(f"The old text appears {count} times in {name}; it must match exactly one place. "
                         f"Read {path} and quote it exactly.")
    changed = text.replace(old, new, 1)
    if name == "memory.md":
        memory.check_text(changed, text)
    return changed


def _only_adds_lines(old: str, new: str) -> bool:
    """Whether `new` keeps every line of `old` in order, only adding lines."""
    kept = iter(new.splitlines())
    return all(line in kept for line in old.splitlines())


def needs_yes(edit: dict, kind: str) -> str | None:
    """Reason the user must approve this edit, or None if JARVIS may apply it itself."""
    if edit.get("flagged"):
        return f"it looks like hidden instructions, so check it with {profile.name()} word for word: {edit['flagged']}"
    if edit["action"] == "skill":
        return "it saves a skill, which changes how JARVIS goes about that kind of request"
    if kind == "research":
        return "it comes from a research task, which reads mail and web pages that could be steering it"
    if kind == "review" and edit["action"] in ("todo_add", "todo_done", "follow_up"):
        return "the learning review guessed it from the conversation, unasked"
    if edit["action"] == "forget":
        return "it deletes lines from long-term memory"
    if edit["action"] == "todo_reword":
        return "it rewords an item on the to-do list"
    if edit["action"] == "project_update":
        return "it changes the project list's settings, which affect how code tasks run"
    if edit["action"] == "edit":
        if not _only_adds_lines(edit["old"], edit["new"]):
            return f"it removes or rewrites text in {edit['file']}"
    return None


def _quote(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def describe(edit: dict) -> str:
    """The edit in a few plain words."""
    match edit["action"]:
        case "project_register":
            return f"add the project {edit['name']} to the project list"
        case "project_update":
            said = [f"{k} to {_quote(', '.join(v) if k == 'aliases' else str(v).lower() if k == 'in_place' else v, 60)}"
                    for k in ("path", "aliases", "about", "in_place") if (v := edit.get(k)) is not None]
            return f"set the project {edit['name']}'s {', '.join(said)}"
        case "todo_add":
            return f"add a to-do: {_quote(edit['item'])}"
        case "todo_done":
            return f"tick off the to-do matching '{_quote(edit['text'], 60)}'"
        case "todo_reword":
            return f"reword the to-do matching '{_quote(edit['text'], 60)}' as: {_quote(edit['item'])}"
        case "remember":
            where = f", under {memory.title(s)}" if (s := edit.get("section")) in memory.SECTIONS else ""
            return f"save to memory{where}: {_quote(edit['fact'])}"
        case "forget":
            return f"delete the memory lines containing '{_quote(edit['text'], 60)}'"
        case "vocabulary":
            heard = f" (heard as {', '.join(edit['heard_as'])})" if edit.get("heard_as") else ""
            return f"add {edit['word']} to the speech vocabulary{heard}"
        case "follow_up":
            at = schedules.clock(schedules.parse_time(edit["when"]).timestamp())
            return f"remind {profile.name()} at {at}: {_quote(edit['text'])}"
        case "skill":
            new = not skills.folder(edit["name"]).is_dir()
            return f"{'save a new skill' if new else 'update the skill'} {edit['name']}: {_quote(edit['description'])}"
        case _:
            if not edit["new"]:
                return f"in {edit['file']}, delete '{_quote(edit['old'], 60)}'"
            return f"in {edit['file']}, replace '{_quote(edit['old'], 60)}' with '{_quote(edit['new'], 60)}'"


def pending(task: dict) -> list[dict]:
    return [e for e in edits(task) if e["status"] == PENDING]


def apply(edit: dict) -> str:
    """Apply the edit; returns the voice tool's reply. ValueError if it can't be done."""
    match edit["action"]:
        case "project_register":
            out = projects.register(edit["name"], edit.get("path") or None, edit.get("aliases"), edit.get("about", ""),
                                    edit.get("in_place", projects.IN_PLACE_DEFAULT))
            return f"Registered {out['name']} at {out['path']}, {projects.mode(out['in_place'])}."
        case "project_update":
            out = projects.update(edit["name"], edit.get("path"), edit.get("aliases"), edit.get("about"), edit.get("in_place"))
            return f"Updated {out['name']}: at {out['path']}, {projects.mode(out['in_place'])}."
        case "todo_add":
            return todo.add(edit["item"])
        case "todo_done":
            return todo.done(edit["text"])
        case "todo_reword":
            return todo.reword(edit["text"], edit["item"])
        case "remember":
            return memory.remember(edit["fact"], edit.get("section", ""))  # missing section fails
        case "forget":
            return memory.forget(edit["text"])
        case "vocabulary":
            return vocabulary.add(edit["word"], edit.get("heard_as", ()), learned=True)
        case "follow_up":  # stored as a reminder job
            job = schedules.add("reminder", "", edit["text"], when=edit["when"])
            return f"I'll remind {profile.name()} {schedules.describe(job)}: {job['text']}"
        case "skill":
            skill, new = skills.save(edit["name"], edit["description"], edit["instructions"], skills.LEARNED)
            state = "switched on" if skill.usable else skill.state
            return (f"Saved the {'new skill' if new else 'new version of the skill'} {skill.name}, {state}, from the "
                    "next conversation.")
        case _:
            path = _file(edit["file"])
            write_atomic(path, _replaced(edit["file"], edit["old"], edit["new"]))
            return f"Edited {edit['file']}."
