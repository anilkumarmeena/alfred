"""Control panel actions: The to-do list."""


from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import todo

# ── To-dos ──────────────────────────────────────────────────────


@action
async def todos(_: dict) -> list[dict]:
    return todo.items()


@action
async def todo_add(body: dict) -> list[dict]:
    todo.add(_text(body, "text", 500))
    return todo.items()


@action
async def todo_check(body: dict) -> list[dict]:
    todo.check(_id(body), bool(body.get("done", True)))
    return todo.items()


@action
async def todo_edit(body: dict) -> list[dict]:
    todo.edit(_id(body), _text(body, "text", 500))
    return todo.items()


@action
async def todo_delete(body: dict) -> list[dict]:
    todo.delete(_id(body))
    return todo.items()


@action
async def todo_restore(body: dict) -> list[dict]:
    """Undo a delete: the item as todo_delete's caller last saw it."""
    todo.restore({"id": _id(body), "text": _text(body, "text", 500), "done": body.get("done") is True,
                  "created": body.get("created"), "done_at": body.get("done_at")})
    return todo.items()
