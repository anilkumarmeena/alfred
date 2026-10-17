"""Control panel actions: Memory, "about you" and the speech vocabulary."""

import asyncio
from collections.abc import Callable

from jarvis import config, projects
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import memory, settings, vocabulary

# ── Memory and "about you" ──────────────────────────────────────


def _memory(change: Callable[[], object] = lambda: None) -> dict:
    """Apply `change`, then return facts by section with usage vs limits. memory.Full becomes a ValueError."""
    try:
        change()
    except memory.Full as e:
        raise ValueError(f"{e.short} Shorten or forget some of its facts first.") from None
    found = memory.facts()
    return found | {"limits": {s: {"used": memory.usage(found[s]), "max": memory.LIMITS[s]} for s in memory.SECTIONS}
                    | {"fact": memory.FACT_MAX}}


@action
async def memory_(_: dict) -> dict:
    return _memory()


@action
async def remember(body: dict) -> dict:
    if not (fact := _text(body, "fact", 1000)):
        raise ValueError("The memory is empty.")
    return _memory(lambda: memory.remember(fact, _text(body, "section")))


@action
async def memory_edit(body: dict) -> dict:
    """Replace one memory line (exact text as listed); an empty fact forgets it."""
    return _memory(lambda: memory.replace_fact(str(body.get("line", "")), _text(body, "fact", 1000)))


# ── Speech vocabulary ───────────────────────────────────────────


def _heard(body: dict) -> list[str]:
    """Mishearings from a list or comma-separated text."""
    value = body.get("heard", [])
    if isinstance(value, str):
        value = value.split(",")
    if not (isinstance(value, list) and all(isinstance(h, str) for h in value)):
        raise ValueError("heard must be text.")
    return [h.strip() for h in value if h.strip()][:vocabulary.MAX_HEARD]


def _vocabulary(change: Callable[[], object] = lambda: None) -> dict:
    """Apply `change`, then return entries, extra project terms, the cap and the prompt setting."""
    change()
    found = vocabulary.entries()
    return {"entries": [{"word": e.word, "heard": e.heard, "date": e.date, "learned": e.learned} for e in found],
            "projects": [p for p in vocabulary.project_terms(projects.load) if not vocabulary.find(found, p)],
            "max": vocabulary.MAX_ENTRIES, "prompt": config.STT_PROMPT}


@action
async def vocabulary_(_: dict) -> dict:
    return await asyncio.to_thread(_vocabulary)


@action
async def vocabulary_add(body: dict) -> dict:
    word, heard = _text(body, "word", 200), _heard(body)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.add(word, heard))


@action
async def vocabulary_save(body: dict) -> dict:
    """Replace an entry (by its current word) with the given word and mishearings."""
    old, word, heard = _text(body, "old", 200), _text(body, "word", 200), _heard(body)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.save(old, word, heard))


@action
async def vocabulary_remove(body: dict) -> dict:
    word = _text(body, "word", 200)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.remove(word))


@action
async def about(_: dict) -> dict:
    return {"text": settings.about()}


@action
async def about_save(body: dict) -> dict:
    return {"text": settings.set_about(_text(body, "text", 4000))}
