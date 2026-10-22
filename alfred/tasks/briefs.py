"""System prompt for a task session: Claude Code's own, plus where it works, what Alfred needs from it, its access,
skills and to-do."""

from collections.abc import Iterable
from pathlib import Path

from alfred import projects, prompts
from alfred.config import ROOT
from alfred.integrations import mcp_servers
from alfred.llm.claude import ATTRIBUTION
from alfred.skills import home as skills
from alfred.state import todo
from alfred.storage import profile
from alfred.tasks import access, repo_files

# Alfred checks a finished change against what its log shows, and a run in the background or a subagent shows no result
FINAL_CHECK = ("\n- After your last edit, run the project's tests (or build) once more yourself, with Bash in the "
               "foreground, not in the background or through a subagent, so Alfred's check of your result sees them "
               "finish; give the outcome (\"212 passed\") in your summary.")


def brief() -> str:
    """Shared brief for every task, for the current profile."""
    mine = profile.current()
    return prompts.load("task_brief", user_name=profile.name(), db_file=mine.db, memory_file=mine.memory_file)




def _google_limits() -> str:
    """What account tools leave to the user (their deletes and sends need a spoken yes)."""
    return ("You can't send mail or Chat messages, send calendar invites, delete sheet tabs or delete doc content: "
            f"leave those for {profile.name()}, and say so in your summary.")


def _connections_brief(read_only: bool = False) -> str:
    """How a connection's reading and changing tools work in a task (a read-only one only reads), and, with a GitLab
    connection on, its download tools and inline comments."""
    name = profile.name()
    changes = ("only reading: a tool that changes something there or reaches other people is refused." if read_only else
               "reading is free. A tool that changes something there or reaches "
               "other people (a comment, an inline review note, an issue, a merge) waits for their yes to that exact call: "
               "just make the call. It's refused for now, and Alfred reads out what it would do and asks them, so don't ask "
               "them yourself. Carry on with anything that doesn't depend on it; if nothing's left, end your turn with one "
               "short line. When they answer you'll get a message; if it's a yes, make the identical call again, with "
               "exactly the same arguments, once. A tool refused as not available can't be used here.")
    said = (f"\n- With the tools {name} has connected, {changes} Never look for their keys or tokens, or post or fetch "
            "another way round (glab, gh, curl, git clone, the API).")
    labs = [s for s in mcp_servers.for_research() if repo_files.ADDRESS in mcp_servers.addresses(s)]
    if repo_files.gitlab(labs):
        said += (f"\n- To read a GitLab file whole, or a whole project, use {' or '.join(repo_files.TOOLS)}: they fetch "
                 "with the connection's own key, which you never need.")
    if labs and not read_only:
        said += (f"\n- An inline comment on a merge request's diff line is GitLab's {mcp_servers.GITLAB_THREAD}, with a "
                 "position: position_type text, base_sha, start_sha and head_sha from the merge request's diff_refs, "
                 "and new_path and new_line (old_path and old_line for a removed line).")
    return said


def _artifact_brief(folder: Path) -> str:
    return (f"\n- When asked for a mockup, a preview or anything else to look at, write it as an HTML file in {folder}, which "
            "stays as the local copy, then publish that file with the Artifact tool and put the link it returns "
            f"(https://claude.ai/code/artifact/...) in your summary, with the file's path: {profile.name()} sees no card, only "
            "your summary. If publishing fails, say why in one line and give the file's path instead. Never delete an artifact.")




def _skills_brief(found: list[skills.Skill]) -> str:
    """List Alfred's own skills loaded into the session."""
    if not found:
        return ""
    return (f"\n- Alfred's own skills are loaded, how-tos {profile.name()} keeps for workflows they repeat. When the work "
            "matches one's description, or the instruction names it, run it with the Skill tool and follow it, with "
            f"these rules still applying:\n{skills.listing(found)}")


def _todo_brief(task: dict) -> str:
    """Note the linked to-do item; Alfred settles it at the end, so the task must not."""
    if not task.get("todo") or not (item := todo.get(task["todo"])):
        return ""
    return (f"\n- This task works on {profile.name()}'s to-do item \"{item['text']}\". Alfred ticks it off, or rewords it to "
            "what's left, when you finish, so don't suggest todo_done or todo_reword for it.")


def _code_brief(task: dict, cwd: Path) -> str:
    """A code change's lines on where it works and what git it may do."""
    if task["in_place"]:
        if not task["base"] and (repos := projects.child_repos(cwd)):
            on = (f"a folder that isn't a git repository itself but holds separate ones, each with its own history: "
                  f"{', '.join(repos)}. Use git inside each repo's folder (git -C <repo> status, diff, log, and commit "
                  "when asked, each repo you changed on its own), never in this parent folder, and don't run git init here")
        elif not task["base"]:
            on = "a folder that isn't a git repository yet. Don't run git init: git gets set up later, when asked for"
        else:
            on = f"on branch {task['branch']}" if task["branch"] else "on a detached HEAD"
        return f"""
- You are working directly in {profile.name()}'s own checkout of {task['project']} at {cwd}, {on}. There is no separate worktree or branch. Read the project's CLAUDE.md or README first if it has one.
- {profile.name()} may have uncommitted changes here. Keep them: never reset, restore, check out, stash or clean files, and don't delete or overwrite work that isn't yours.
- Don't push, or create or switch branches. Commit only when {profile.name()}'s own request asks you to (it reaches you through Alfred, as with "fix it and commit"): then a plain git commit with Bash goes through, never --amend. Stage only the files you changed, never their own uncommitted work, and end the message with "{ATTRIBUTION['commit']}". Otherwise leave your changes uncommitted for {profile.name()} to review. Either way, list the files you changed in your summary."""
    return f"""
- You are in a fresh git worktree of {task['project']}, on branch {task['branch']}, made from commit {task['base'][:7]}. Read the project's CLAUDE.md or README first if it has one.
- Gitignored files (.env, build output) aren't here. Dependencies may be: node_modules and .venv folders the main checkout has are cloned in, as they were there when this worktree was made. Check before installing, and install only what's missing or out of date; changes to them stay in this worktree.
- Don't commit, push or switch branches. Alfred commits your work to {task['branch']} when you finish."""


def _system_prompt(task: dict, cwd: Path, own: Path, can: set[str], mine: Iterable[skills.Skill] = ()) -> dict:
    """A task's system prompt: Claude Code's own, plus where the task works, what Alfred needs from it, and the access
    it can still ask for. `own` is its output folder; `mine` are Alfred skills loaded into the session."""
    kind, name, read_only = task["kind"], profile.name(), bool(task.get("read_only"))
    if kind == "code_change":
        where = _code_brief(task, cwd)
    elif kind == "code_question":
        where = (f"\n- You're answering a question about {task['project']} at {cwd}, {name}'s live checkout. Read, search "
                 "and run what the answer needs, but leave its files as they are unless the question asks for a change. "
                 f"Your own folder is {own}: keep anything you make there.")
    else:
        where = (f"\n- You research and gather information for {name}, with the web, the computer and their tools. Your "
                 f"own folder is {own}: keep what you make there.")
    lines = [f"""
- You run in Claude Code's auto mode with {name}'s own Claude Code setup loaded (their CLAUDE.md, skills, plugins and agents), and nothing is sandboxed. A separate check clears each action as you go: when it blocks one, take another reasonable way or leave it and say why in your summary, never work around it. When it stops to ask, Alfred asks {name} on their phone, and you get a message once they answer.
- Never stop or start Alfred's own services yourself (launchctl, systemctl --user, killing its processes): you run inside them, and a stop without its start leaves Alfred down. On a Mac, `make -C {ROOT} services-restart` restarts them safely; on Linux, end your summary with that command for {name} to run."""]
    if read_only:
        lines.append("\n- You only find things out: read, search and look, but don't edit files, publish or change "
                     "anything, and you never need to.")
    if "accounts" in can:
        edits = "" if read_only else f", and edit their Google Sheets and Docs. {_google_limits()}"
        lines.append(f"\n- You can read {name}'s mail, calendar, Google Chat, Google Sheets, Docs, Slides and Drive, and the "
                     f"tools they've connected (mcp__<server>__<tool>, loaded with ToolSearch when you need one){edits or '.'}"
                     + _connections_brief(read_only))
    if lack := access.missing(task):
        lines.append(f"\n- If {name} asked for something that needs access you don't have, ask for it with request_access "
                     f"rather than giving up: {access.listing(lack)}. Then end your turn with one short line; once it's "
                     f"granted you carry on in this same session, with it. Ask only for what {name}'s request needs.")
    pages = _artifact_brief(own) if "artifacts" in can else ""
    final = FINAL_CHECK if kind == "code_change" else ""
    return {"type": "preset", "preset": "claude_code", "append": brief() + where + "".join(lines) + pages
            + _skills_brief(list(mine)) + _todo_brief(task) + final}

