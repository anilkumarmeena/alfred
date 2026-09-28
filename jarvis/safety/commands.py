"""What JARVIS makes of shell commands, and of the yes or no it hears about them. Shared by the voice
shell (jarvis.safety.shell) and by background tasks' host commands (jarvis.tasks.worker, jarvis.safety.host).

  risky(command)      why it needs the user's yes first ('deletes files', 'installs software', ...), or ''
  risky(command, own) the same for a background task, for which deleting its own files and stopping its own
                      processes (Own) needs no yes
  describe(command)   that reason with the program it comes from, for speaking: 'installs software (brew install)'
  refused(command)    why a task may never run it, even with a yes (secrets, JARVIS's own controls, sudo), or ''
  own_sandbox(command)  whether it's only a package install that sandboxes its own steps (brew install), so a task's
                      approved one runs outside the host profile
  names_secret(text)  whether text names a secret: .env files, keys, logins, secrets folders
  reads_environment(command)  whether it reads other processes' environments (ps eww), where keys are
  secret_path(path)   whether a (resolved) file path is one
  approves(said), refuses(said)   whether a spoken reply is a clear yes, or a clear no, from word lists; a reply
                      they can't read goes to a short model call (jarvis.safety.consent)
  tells_approve(said), tells_deny(said)   whether the user's words plainly tell JARVIS to approve, or turn down,
                      a task's waiting command, later in the conversation (tools.tasks.task_approve)
  ordered(said, command)  whether the user's own words order just what a risky command does ("push it" for git
                      push), which is their go-ahead for it; agreed_to(asked, said, command) the same for a clear yes
                      to JARVIS's question asking to do it
  same_risk(a, b)     whether two command lines do the same risky things in the same folders, so a yes to one does

The secret lists here are the one copy: the voice shell's deny rules, the task file tools' checks and
the host profile (jarvis.safety.host) are all built from them.
"""

import fnmatch
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.config import CLAUDE_CONFIG_DIR, DATA_DIR, ROOT, SECRETS_DIR

# ── Secrets ─────────────────────────────────────────────────────

# In the home folder: logins, keys and the keychains
HOME_SECRETS = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.docker/config.json",
                "~/Library/Keychains", "~/.git-credentials", "~/.npmrc", "~/.pypirc"]
# JARVIS's own: its keys and tokens (every profile's sign-ins are inside secrets/), its Claude login (with the voice
# session logs), its .env, and the owner's web browser profile, whose cookies keep their logins
# (jarvis.integrations.browser; jarvis.core.profile.Profile.browser_profile)
JARVIS_SECRETS = [SECRETS_DIR, Path(CLAUDE_CONFIG_DIR), ROOT / ".env", DATA_DIR / "browser/profile"]
# Folders and files that are secret wherever they are
SECRET_DIRS = {"secrets", ".ssh", ".aws", ".gnupg", "Keychains"}


def secret_file_names(char: str = ".", pem: bool = True) -> str:
    """The secret file names as one regex alternation. `char` is what a wildcard character matches: '[^/]' keeps
    a match within one path component. `pem=False` leaves out .pem files, for callers that treat them apart."""
    keys = "pem|key" if pem else "key"
    return (rf"\.env(\.{char}*)?|{char}+\.({keys}|p8|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)|\.netrc|\.npmrc"
            rf"|\.pypirc|\.git-credentials|credentials(\.json)?|{char}*client_secret{char}*\.json|tokens?\.json")


SECRET_FILES = re.compile(secret_file_names(), re.IGNORECASE)
# The same files as glob patterns, for Claude Code's Read deny rules
SECRET_PATTERNS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.pfx", "**/id_rsa",
                   "**/id_dsa", "**/id_ecdsa", "**/id_ed25519", "**/.netrc", "**/.npmrc", "**/.pypirc",
                   "**/.git-credentials", "**/credentials", "**/credentials.json", "**/*client_secret*.json",
                   "**/token.json", "**/tokens.json"]

# A command whose text names one of these is refused even with a yes: outside a sandbox, no deny rule covers it
_SECRET_NAMES = re.compile(
    "|".join(re.escape(str(p)) for p in JARVIS_SECRETS)
    + "|" + "|".join(re.escape(p.removeprefix("~/")) for p in HOME_SECRETS)
    + r"|\.env(?!\w)|secrets/|\bid_(rsa|dsa|ecdsa|ed25519)\b(?!\.pub)|\.(pem|key|p8|p12|pfx)\b|client_secret"
    + r"|JARVIS_MASTER_KEY"
)


def names_secret(text: str) -> bool:
    """Whether a command (or any text) names a secret: a secrets folder, a .env file, a key or a login."""
    return bool(_SECRET_NAMES.search(text))


# Other processes' environments hold their keys: an MCP server gets its token there (jarvis.integrations.mcp_launch). Only the
# setuid /bin/ps can read another process's environment on macOS (the kernel shows a process only its own), and
# Linux keeps it in /proc/<pid>/environ.
_ENV_TEXT = re.compile(r"/proc/[^\s/]+/environ|\bprocargs|\blaunchctl\s+procinfo\b")
_PS_VALUES = set("oOpgGtuUMN")  # ps options that take a value, after which a cluster's letters stop being flags


def _ps_shows_env(args: list[str]) -> bool:
    for n, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--"):
            letters = re.split(f"[{''.join(_PS_VALUES)}]", arg[1:], maxsplit=1)[0]
            if set(letters) & {"e", "E"}:
                return True
        elif n == 0 and re.fullmatch(r"[A-Za-z]+", arg) and "e" in arg:  # BSD style: ps eww, ps auxe
            return True
    return False


def reads_environment(command: str) -> bool:
    """Whether a command reads other processes' environments (ps eww, ps -E, /proc/<pid>/environ, ...), where
    MCP servers' tokens are. Refused even with a yes, like secrets."""
    if _ENV_TEXT.search(command):
        return True
    for argv in _simple_commands(command) or []:
        argv = _strip_prefixes(argv)
        while argv and Path(argv[0]).name in ("sudo", "doas"):
            argv = _strip_prefixes(_options_then(argv[1:], {"-u", "-g", "-C", "-h", "-p", "-U"}))
        if argv and Path(argv[0].lstrip("\\")).name == "ps" and _ps_shows_env(argv[1:]):
            return True
        if argv and Path(argv[0]).name in ("lldb", "gdb") and any(a in ("-p", "--attach-pid", "--pid") or
                                                                     a.startswith("--attach") for a in argv[1:]):
            return True
    return False


def secret_path(path: Path) -> bool:
    """Whether `path` (resolved) is a secret: in a secrets folder or a login's folder, a .env file, a key or a credential."""
    if any(path.is_relative_to(Path(d).expanduser().resolve()) for d in (*JARVIS_SECRETS, *HOME_SECRETS)):
        return True
    return bool(SECRET_DIRS.intersection(path.parts)) or bool(SECRET_FILES.fullmatch(path.name))


# ── What counts as a yes, or a no ───────────────────────────────

# A reply is a yes only if, once filler is dropped, it's made up entirely of these phrases.
# Anything else (a question, a no, a "but", a change of subject) is not a yes by these lists, which are only the fast
# path: what they can't read goes to the model (jarvis.safety.consent), unless it's a clear no here.
_AFFIRMATIVE = sorted(
    (tuple(p.split()) for p in """
    yes | yeah | yea | yah | ya | yep | yup | sure | sure thing | ok | okay | okey | okie dokie | alright | all right
    | right | that's right | correct | fine | that's fine | great | good | cool | perfect | lovely | brilliant | excellent
    | absolutely | definitely | certainly | of course | indeed | affirmative | sounds good | sounds great | sounds fine
    | looks good | works for me | that works | go | go ahead | go on | go for it | carry on | proceed | continue
    | keep going | keep at it | keep it going | go on then | on you go | let's go | let's do it | do it | do that | do so
    | please do | confirm | confirmed | approved | approve | i approve | i agree | agreed | granted | permission granted
    | you have my permission | you've got my permission | it is allowed | it's allowed | that is allowed | that's allowed
    | allowed | you're allowed | you are allowed | allow it | you can | you may | yes you can | you can do it
    | all | all of them | both | both of them | all two | all three | all four | all five | the lot | now | right now
    | mm hmm | mm hm | mmm hmm | m hmm | m hm | uh huh | start | start going | get going | get started | begin | go go
    | i'm sure | i am sure | i'm certain | i am certain | for sure | you bet | by all means | go right ahead | totally
    | that's okay | that's ok | that is fine | that is okay | it's fine | it's okay | it's ok | that's good | fine by me
    | fine with me | okay by me | ok by me | i'm okay with that | i'm ok with that | i'm fine with that | i'm good with that
    | let's proceed | let's continue | let's carry on | let's go ahead | let's go for it | we can proceed
    | we can go ahead | we can continue | you can proceed | you can go ahead | you can continue | you can go
    | yes please | accepted | accept | i accept | accept it | all accepted | approve it | approve them | approve all
    | approve both
    """.split("|")),
    key=len,
    reverse=True,
)
_ACTION = re.compile(  # "yes, merge it": an action on what was asked about counts as "go"
    r"\b(let's |let it |you can )?(run|merge|do|install|execute|apply|start|restart|launch|open|try|send|fetch|pull|push"
    r"|update|upgrade|kill|commit|deploy|build|delete|remove|stop|allow|render|close|quit|clean|clear|end|wipe|trash|shut)"
    r" (it|them|that|those|this|these|both|all)( up| down| off)?\b"
)
# "let's close the hidden Chrome": the same, naming what it acts on in a few plain words, with the verbs a risky
# command's question is about. Never with "for", "from", "my" and the like, which make it a request of its own.
_ACTION_ON = re.compile(
    r"\b(let's |let it |you can )?(delete|remove|kill|stop|close|quit|clean up|clear|wipe|trash|shut down|install|push"
    r"|merge|run|restart|end) the( (?!(for|from|to|on|in|at|with|my|me|your|about|of|and|then|instead)\b)[a-z']+){1,3}\b"
)
# Phrases with a "no" or "not" in them that mean yes
_YES_IDIOMS = re.compile(r"\b(why not|no problem|not a problem|no worries|no objection|no objections)\b")
# Sounds speech recognition spells with a hyphen: "Mm-hmm" and "uh-huh" are a yes, "uh-uh" and "mm-mm" a no
_YES_SOUNDS = re.compile(r"\b(m+-?h+m+|uh-huh)\b")
_NO_SOUNDS = re.compile(r"\b(uh-uh|mm+-mm+|nuh-uh|unh-unh)\b")
_FILLER = set("um umm uh uhh uhm er erm ah oh hmm mm well so and then now just sir jarvis please thanks thank you".split())
_REFUSAL = set("""no nope nah not don't dont never stop wait hold hang cancel abort forget skip leave later instead
rather but except only if unless actually first before why what how when where who which isn't shouldn't won't
can't cannot""".split())
# A clear no: an utterance that opens with one of these (after filler), or a short one containing one
_NO_OPENERS = [tuple(p.split()) for p in """
    no | nope | nah | don't | dont | do not | never | never mind | nevermind | cancel | abort | stop | negative
    | forget it | forget that | not now | not yet | not that | leave it | skip it | hold off | i said no | absolutely not
    | definitely not | certainly not | of course not | no way | not really | not okay | not ok | that's not okay
    | that's not ok | that's not fine | i'd rather not | i would rather not | rather not | better not | i'd prefer not
    | i would prefer not | prefer not | deny | deny it | decline | decline it | refuse | reject | don't bother
    """.split("|")]
_NO_WORDS = {"no", "nope", "nah", "don't", "dont", "cancel", "abort", "never"}
_NO_LEAD = _FILLER | {"actually", "ok", "okay", "oh", "sorry"}


def _words(said: str) -> list[str]:
    text = said.lower().replace("’", "'")
    text = re.sub(r"\bo\.?\s?k\b\.?", "ok", text)
    text = _YES_SOUNDS.sub(" yes ", _NO_SOUNDS.sub(" no ", text))
    return re.findall(r"[a-z']+", text)


def approves(said: str) -> bool:
    """Whether a spoken reply is a clear yes: "mm-hmm", "I'm sure", "yeah, let's close the hidden Chrome, that's
    fine". It comes from speech recognition, so it may carry punctuation, filler or "O.K."; anything short of a clear
    yes isn't one, and nor is a lone "thank you" or "bye"."""
    if "?" in said:
        return False
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    if not words or len(words) > 12 or _REFUSAL.intersection(words):
        return False
    words = _ACTION_ON.sub("go", _ACTION.sub("go", " ".join(words))).split()
    agreed, i = False, 0
    while i < len(words):
        if phrase := next((p for p in _AFFIRMATIVE if tuple(words[i : i + len(p)]) == p), None):
            agreed, i = True, i + len(phrase)
        elif words[i] in _FILLER:
            i += 1
        else:
            return False
    return agreed


def refuses(said: str) -> bool:
    """Whether a spoken reply is clearly a no, which drops any question still open. A "wait", a
    question or a change of subject isn't: the question stays open for a yes later."""
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    if len(words) <= 4 and _NO_WORDS.intersection(words):
        return True
    while words and words[0] in _NO_LEAD:
        words = words[1:]
    return any(tuple(words[: len(p)]) == p for p in _NO_OPENERS)


# ── Telling JARVIS to answer a task's request ───────────────────

# Said later in the conversation, not straight after JARVIS's question, so it may name what it's about ("let it
# close the hidden Chrome"): looser than approves and refuses, but still a short, plain instruction.
_TELL_MOST_WORDS = 20
_TELL_APPROVE = re.compile(
    r"\b(approve[ds]?|allow|permit|authori[sz]e|let (it|them|that|task|the task)|go ahead|go for it|green light"
    r"|ok it|okay it|yes|yeah|yep|sure|run it|do it|proceed|carry on)\b"
)
_TELL_DENY = re.compile(
    r"\b(deny|denied|decline|refuse|reject|block it|say no|tell it no|(don't|dont|do not) (let|allow|approve|run|do)"
    r"|no|nope|nah|not allowed|cancel)\b"
)
# Any of these makes it more than a plain yes: a no, a condition, a doubt or a "wait"
_TELL_NOT_YES = set("""no nope nah not don't dont never wait hold hang cancel abort unless if but except deny denied
decline refuse reject can't cannot won't shouldn't isn't later maybe perhaps""".split())


def _told(said: str) -> list[str]:
    """The words of a short instruction that isn't a question, with "no worries" and the like read as yes; else []."""
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    return [] if "?" in said or len(words) > _TELL_MOST_WORDS else words


def tells_approve(said: str) -> bool:
    """Whether the user's words plainly tell JARVIS to approve a task's waiting command: 'approve it',
    'let it close the hidden Chrome', 'yes, go ahead with that delete', 'approve it, and don't ask again'. Never a
    question, a no, an "if" or a "wait"."""
    if "?" in said:
        return False
    found, rest = _scoped(said)
    words = _told(rest)
    if not words and found:  # "always allow it": the scope phrase says it
        words = _told(said)
        return not _TELL_NOT_YES.intersection(words) and bool(_ALWAYS_ALLOW.search(" ".join(words)))
    return bool(words) and not _TELL_NOT_YES.intersection(words) and bool(_TELL_APPROVE.search(" ".join(words)))


# ── How long a yes lasts ────────────────────────────────────────
# A yes can say how far it goes (jarvis.safety.remembered): just this once, for this session (the rest of the
# conversation, or of the task that asked), or always for that kind of command. Some phrases are a yes on their own
# ("always", "for this session"); others only beside one ("yes, and don't ask again"), since "don't ask me again"
# alone might be a no.

SCOPES = ("once", "session", "always")  # narrowest first
_SCOPE_PHRASES = [  # (scope, a yes on its own, the phrase)
    ("once", True, r"(just |only )?(this|that) (once|one time|time)|(just |only )?once"),
    ("session", True, r"(for )?(the rest of )?(this|the) (session|conversation|call|task)"),
    ("always", True, r"always( (allow|approve|let) (it|that|them|this))?( for (this|that) kind of (command|commands|thing))?"),
    ("always", False, r"from now on|every time|for good|permanently|(and )?(don't|dont|do not|never) ask( me)?( about)?"
                      r"( (it|that|this|them))? again|(for )?(this|that) kind of (command|commands|thing)"),
]
_SCOPE_RE = [(scope, alone, re.compile(rf"\b({phrase})\b")) for scope, alone, phrase in _SCOPE_PHRASES]
_ALWAYS_ALLOW = re.compile(r"\balways (allow|approve|let) (it|that|them|this)\b")
_BROAD = re.compile(r"\b(kind|kinds|any|anything|whatever)\b")  # "always, any brew install": the kind, not the one


def _scoped(said: str) -> tuple[list[tuple[str, bool]], str]:
    """The scope phrases in the user's words, each (scope, whether it's a yes on its own), and the words without them."""
    text, found = " ".join(_words(said)), []
    for scope, alone, phrase in _SCOPE_RE:
        if phrase.search(text):
            found.append((scope, alone))
            text = phrase.sub(" ", text)
    return found, " ".join(text.split())


def yes_scope(said: str) -> str | None:
    """For a clear yes, how far it goes: 'once' (the default, and whenever the words disagree), 'session' or
    'always'. "Yes, always", "for this session", "just this once", "go ahead, and don't ask again". None for
    anything that isn't a clear yes."""
    if "?" in said:
        return None
    found, rest = _scoped(said)
    if not [w for w in rest.split() if w not in _FILLER]:
        if not found or not all(alone for _, alone in found):
            return None
    elif not approves(rest):
        return None
    return _narrowest(found)


def said_scope(said: str) -> str:
    """How far the user's words say a yes goes, from their scope phrases alone: 'once' unless they say more."""
    return _narrowest(_scoped(said)[0])


def scopes_said(said: str) -> list[str]:
    """The scopes the user's words name with a scope phrase ('once', 'session', 'always'), if any."""
    return [scope for scope, _ in _scoped(said)[0]]


def _narrowest(found: list[tuple[str, bool]]) -> str:
    """The narrowest of the scopes said (they may disagree), or 'once' if none was."""
    return min((scope for scope, _ in found), key=SCOPES.index, default="once")


def said_breadth(said: str) -> str:
    """For an "always": 'kind' if the user's words say any command of that kind ("always, for anything like
    that", "always allow this kind of command"), else 'exact', that command as it is."""
    return "kind" if _BROAD.search(" ".join(_words(said))) else "exact"


def tells_deny(said: str) -> bool:
    """Whether the user's words plainly tell JARVIS to turn a task's waiting command down: 'deny it', 'no, don't
    let it delete that', 'decline task 5's request'. Never a question."""
    words = _told(said)
    return bool(words) and (refuses(said) or bool(_TELL_DENY.search(" ".join(words))))


# ── What counts as risky ────────────────────────────────────────

# A command line is split into simple commands at ; & | && || ( ) and newlines, and each is judged by
# its program and arguments. What a shell, eval or an interpreter would run from a string can't be
# judged, so those count as risky themselves.
_PREFIXES = {"nohup", "time", "exec", "command", "builtin", "env", "nice", "caffeinate", "xargs", "timeout", "gtimeout",
             "stdbuf", "then", "do", "else", "elif", "if", "while", "until", "!", "noglob", "{", "}"}
_PREFIX_VALUES = {"-I", "-J", "-L", "-n", "-P", "-R", "-s", "-E", "-d", "-u", "-t", "-w"}  # xargs -I {}, nice -n 5, ...
_SUBSTITUTIONS = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_FD_NUMBER = re.compile(r"(?<![^\s;&|()])\d+(?=[<>])")
_ALWAYS = {
    "rm": "deletes files", "rmdir": "deletes files", "unlink": "deletes files", "shred": "deletes files",
    "srm": "deletes files", "trash": "deletes files", "truncate": "deletes files", "dd": "writes a disk",
    "kill": "stops a process", "killall": "stops a process", "pkill": "stops a process",
    "shutdown": "stops the Mac", "reboot": "stops the Mac", "halt": "stops the Mac",
    "sudo": "runs as root", "su": "runs as root", "doas": "runs as root",
    "chmod": "changes permissions", "chown": "changes permissions", "chgrp": "changes permissions",
    "chflags": "changes permissions", "crontab": "changes the system", "installer": "installs software",
    "softwareupdate": "installs software", "pipx": "installs software", "mas": "installs software",
    "npx": "installs software", "pnpx": "installs software", "bunx": "installs software", "uvx": "installs software",
    "port": "installs software", "apt": "installs software", "apt-get": "installs software",
    "scp": "sends data off the Mac", "sftp": "sends data off the Mac", "ssh": "sends data off the Mac",
    "ftp": "sends data off the Mac", "nc": "sends data off the Mac", "ncat": "sends data off the Mac",
    "netcat": "sends data off the Mac", "telnet": "sends data off the Mac", "mail": "sends data off the Mac",
    "sendmail": "sends data off the Mac", "csrutil": "changes the system",
    "spctl": "changes the system", "tccutil": "changes the system", "dscl": "changes the system",
    "tmutil": "changes the system", "osascript": "controls apps",
    "eval": "runs code it can't see", "watch": "runs code it can't see", "script": "runs code it can't see",
}
_READ_ONLY_SUBCOMMANDS = {  # system tools whose other subcommands change things
    "launchctl": {"list", "print", "print-disabled", "blame", "managername", "version", "getenv", "help"},
    "diskutil": {"list", "info", "activity", "listFilesystems"},
    "defaults": {"read", "read-type", "domains", "find", "help"},
    # security reads the keychain; listing signing identities and decoding a provisioning profile don't
    "security": {"find-identity", "cms"},
}
_READ_ONLY_WHY = {"security": "reads the keychain"}
_INSTALLS = {
    "brew": {"install", "uninstall", "remove", "rm", "reinstall", "upgrade", "tap", "untap", "link", "unlink", "cleanup",
             "autoremove", "pin", "unpin", "bundle"},
    "pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"},
    "npm": {"install", "i", "ci", "add", "uninstall", "remove", "rm", "un", "update", "upgrade", "link", "publish", "unpublish",
            "exec", "x"},
    "pnpm": {"install", "i", "add", "remove", "rm", "uninstall", "update", "up", "link", "publish", "dlx"},
    "yarn": {"install", "add", "remove", "upgrade", "global", "link", "publish", "dlx"},
    "bun": {"install", "i", "add", "remove", "rm", "update", "link", "publish", "x"},
    "gem": {"install", "uninstall", "update"}, "cargo": {"install", "uninstall", "add", "remove"},
    "go": {"install", "get"}, "poetry": {"install", "add", "remove", "update"},
    "conda": {"install", "remove", "uninstall", "update", "create"}, "mamba": {"install", "remove", "uninstall", "update", "create"},
    "uv": {"add", "remove", "sync"},
}
_GH_WRITES = {"create", "merge", "delete", "close", "edit", "comment", "review", "reopen", "archive", "unarchive", "transfer",
              "rename", "sync", "fork", "lock", "unlock", "set", "remove", "add", "upload", "deploy", "rerun", "cancel",
              "enable", "disable", "pin", "unpin", "ready", "revert"}
# glab's are gh's, and its own words for approving, commenting on and running things
_GLAB_WRITES = _GH_WRITES | {"update", "approve", "revoke", "note", "rebase", "subscribe", "unsubscribe", "retry", "run",
                             "trigger", "mirror", "todo", "publish", "star", "unstar"}
# Config that makes git run a program later: aliases with !, core.pager, core.fsmonitor, core.sshCommand, hooksPath,
# included files, filters and diff and merge drivers, credential helpers, ...
_GIT_CODE_CONFIG = {"alias", "core", "include", "includeif", "filter", "diff", "difftool", "merge", "mergetool",
                    "credential", "gpg", "sequence", "pager", "interactive", "uploadpack", "receive"}
_GIT_CONFIG_VALUES = {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value", "--url"}
_GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool", "-l",
                     "--list"}
_GIT_CONFIG_WRITES = {"--add", "--replace-all", "--unset", "--unset-all", "--rename-section", "--remove-section"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_INLINE_CODE = {"python": {"-c", "-"}, "node": {"-e", "--eval", "-p", "--print"}, "perl": {"-e", "-E"}, "ruby": {"-e"},
                "php": {"-r"}, "deno": {"eval"}}
_CLOBBER_DOTFILE = re.compile(r"(?<![0-9&])>>?\s*(?!/dev/)(\S*/)?\.[A-Za-z]")  # > ~/.zshrc, >> .env
# The iPhone and its simulators: installing a build onto the phone is ordinary, these aren't
_DEVICE_CHANGES = {"uninstall", "terminate", "reboot", "shutdown", "unpair", "erase", "signal", "delete"}
_XCRUN_VALUES = {"--sdk", "-sdk", "--toolchain", "-toolchain"}


def _options_then(argv: list[str], takes_value: set[str] = frozenset()) -> list[str]:
    """argv without its leading options (and the values of those in `takes_value`)."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in takes_value else 1
    return argv[i:]


def _git_config(args: list[str]) -> str:
    """Why a git config command is risky: it edits the file in an editor, or writes config that runs code."""
    words, i = [], 0
    while i < len(args):
        if args[i] in _GIT_CONFIG_VALUES:
            i += 1
        elif not args[i].startswith("-"):
            words.append(args[i])
        i += 1
    flags = set(args)
    if flags & {"-e", "--edit"} or words[:1] == ["edit"]:
        return "runs code it can't see"
    if words[:1] in (["get"], ["list"]) or flags & _GIT_CONFIG_READS:
        return ""
    if words[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"]):
        words = words[1:]
    elif not flags & _GIT_CONFIG_WRITES and len(words) < 2:
        return ""  # git config core.pager: a read
    if any(w.split(".")[0].lower() in _GIT_CODE_CONFIG for w in words):
        return "runs code it can't see"
    return ""


def _git(args: list[str]) -> str:
    # git -c core.pager=..., -c alias.x='!cmd': config given on the command line can run anything
    leading = args[: len(args) - len(_options_then(args, {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}))]
    if any(a in ("-c", "--config-env") or a.startswith("--config-env=") for a in leading):
        return "runs code it can't see"
    rest = args[len(leading):]
    if not rest:
        return ""
    sub, args = rest[0], rest[1:]
    flags = set(args)
    if sub == "config":
        return _git_config(args)
    if "--ext-diff" in flags or sub == "difftool" and any(
            a in ("-x", "--extcmd") or a.startswith(("-x", "--extcmd=")) for a in args):
        return "runs code it can't see"
    if sub in ("push", "clean", "filter-branch", "filter-repo", "rm"):
        return "pushes" if sub == "push" else "deletes files"
    if (sub == "reset" and "--hard" in flags
            or sub == "branch" and flags & {"-D", "-d", "--delete", "-M", "-f", "--force"}
            or sub == "stash" and args[:1] in (["drop"], ["clear"])
            or sub == "checkout" and flags & {"-f", "--force", "--", "."}
            or sub == "restore" and not ("--staged" in flags and not flags & {"--worktree", "-W"})
            or sub == "reflog" and args[:1] in (["expire"], ["delete"])
            or sub == "gc" and any(a.startswith("--prune") for a in args)
            or sub == "update-ref" and "-d" in flags
            or sub == "worktree" and args[:1] in (["remove"], ["prune"])
            or sub == "tag" and flags & {"-d", "--delete", "-f", "--force"}
            or sub == "remote" and args[:1] in (["remove"], ["rm"], ["set-url"])):
        return "discards work"
    return ""


def _code_host(args: list[str], host: str, credentials: set[str], writes: set[str]) -> str:
    """gh and glab: what a command does to `host`, or to the CLI's own credentials."""
    rest = _options_then(args, {"-R", "--repo"})
    sub = rest[0] if rest else ""
    if sub in credentials:
        return "touches credentials"
    if sub == "api":
        method = next((b for a, b in zip(args, args[1:]) if a in ("-X", "--method")), "") or next(
            (a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")
        if method.upper() not in ("", "GET", "HEAD") or any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args):
            return f"changes things on {host}"
        return ""
    if writes.intersection(rest[1:3]):
        return f"changes things on {host}"
    return ""


def _gh(args: list[str]) -> str:
    if "workflow" in _options_then(args, {"-R", "--repo"})[:1] and "run" in args:
        return "changes things on GitHub"
    return _code_host(args, "GitHub", {"auth", "secret", "ssh-key", "gpg-key"}, _GH_WRITES)


def _glab(args: list[str]) -> str:
    return _code_host(args, "GitLab", {"auth", "ssh-key", "gpg-key", "deploy-key", "token", "variable"}, _GLAB_WRITES)


def _curl(args: list[str]) -> str:
    uploads = ("-d", "--data", "-F", "--form", "-T", "--upload-file", "--json")
    if any(a.startswith(uploads) for a in args):
        return "sends data off the Mac"
    method = next((b for a, b in zip(args, args[1:]) if a in ("-X", "--request")), "") or next(
        (a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")
    return "sends data off the Mac" if method.upper() not in ("", "GET", "HEAD") else ""


def _strip_prefixes(argv: list[str]) -> list[str]:
    """argv without what only wraps the real command: nohup, env, VAR=value, xargs -I {}, timeout 5, ..."""
    while argv and (argv[0] in _PREFIXES or re.fullmatch(r"\w+=.*", argv[0])):
        argv = argv[1:]
        while argv and (argv[0].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", argv[0])):
            argv = argv[2:] if argv[0] in _PREFIX_VALUES else argv[1:]
    if argv and Path(argv[0]).name == "xcrun":  # xcrun devicectl ...: the tool it finds is the command
        return _strip_prefixes(_options_then(argv[1:], _XCRUN_VALUES))
    return argv


def _piece(argv: list[str]) -> str:
    """Why one simple command is risky, or ''."""
    argv = _strip_prefixes(argv)
    if not argv:
        return ""
    program, args = Path(argv[0].lstrip("\\")).name, argv[1:]
    flags = set(args)
    if program in _ALWAYS or program.startswith(("mkfs", "newfs")):
        return _ALWAYS.get(program, "writes a disk")
    if program in _READ_ONLY_SUBCOMMANDS:
        sub = _options_then(args)
        return "" if sub and sub[0] in _READ_ONLY_SUBCOMMANDS[program] else _READ_ONLY_WHY.get(program, "changes the system")
    if program == "pmset" and "-g" not in flags or program == "networksetup" and not any(
            a.startswith(("-get", "-list", "-print")) for a in args) or program == "xattr" and flags & {"-d", "-c", "-w"}:
        return "changes the system"
    if program in ("devicectl", "simctl") and flags & _DEVICE_CHANGES:
        return "changes the iPhone" if program == "devicectl" else "changes a simulator"
    if program == "brew" and args[:1] == ["services"]:
        return "stops a process" if flags & {"stop", "kill", "restart", "start", "run", "cleanup"} else ""
    if re.fullmatch(r"python[\d.]*", program):
        if args[:2] == ["-m", "pip"] and flags & {"install", "uninstall"}:
            return "installs software"
        program = "python"
    if program in _INSTALLS:
        rest = _options_then(args, {"-C", "--prefix", "--directory", "--cwd"})
        if (program == "yarn" and not rest) or (rest and rest[0] in _INSTALLS[program]):
            return "installs software"
        if program == "uv" and rest[:1] in (["pip"], ["tool"]) and set(rest[1:2]) & {"install", "uninstall", "sync", "upgrade",
                                                                                     "run"}:
            return "installs software"
        return ""
    if program in ("docker", "podman"):
        return "stops a process" if flags & {"stop", "kill", "rm", "rmi", "prune", "down", "restart", "pause"} else ""
    if program == "git":
        return _git(args)
    if program == "gh":
        return _gh(args)
    if program == "glab":
        return _glab(args)
    if program == "curl":
        return _curl(args)
    if program == "wget" and any(a.startswith(("--post-", "--method", "--body-")) for a in args):
        return "sends data off the Mac"
    if program == "rsync":
        if any(a.startswith(("--delete", "--remove-source-files")) for a in args):
            return "deletes files"
        return "sends data off the Mac" if any(re.match(r"[\w.@-]+:", a) for a in args if not a.startswith("-")) else ""
    if program == "find":
        if "-delete" in flags:
            return "deletes files"
        runs = [i for i, a in enumerate(args) if a in ("-exec", "-execdir", "-ok", "-okdir")]
        return next((why for i in runs if (why := _piece(args[i + 1 :]))), "")
    if program in _SHELLS and (not _options_then(args) or any(re.fullmatch(r"-\w*c\w*", a) for a in args)):
        return "runs code it can't see"
    if program in _INLINE_CODE and (not args or flags & _INLINE_CODE[program]):
        return "runs code it can't see"
    return ""


def _simple_commands(command: str) -> list[list[str]] | None:
    """Each simple command in a command line, as argv, those in $(...) and `...` included; None if it can't be
    parsed (unbalanced quotes). Redirections are left out: the word after >, 2>&1 or < isn't an argument."""
    found: list[list[str]] = []
    command = _FD_NUMBER.sub(" ", command)  # the 2 in 2>/dev/null says which output, and isn't an argument
    for inner in _SUBSTITUTIONS.findall(command):  # $(...) and `...` run even inside double quotes
        if (more := _simple_commands("".join(inner))) is None:
            return None
        found += more
    lexer = shlex.shlex(command.replace("\n", " ; ").replace("`", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes
        return None
    argv: list[str] = []
    redirect = False
    for token in tokens + [";"]:
        if redirect:
            redirect = False
        elif token and set(token) <= set("<>") | set(";&|()") and set(token) & set("<>"):
            redirect = True  # >, 2>&1, <: the next word is where output goes, not an argument
        elif token and set(token) <= set(";&|()"):
            if argv:
                found.append(argv)
            argv = []
        else:
            argv.append(token)
    return found


def risky_pieces(command: str, own: "Own | None" = None) -> list[tuple[list[str], str]]:
    """Each risky simple command in a command line, as (its argv without what only wraps it, why it's risky), in
    order; [] if it can simply run. With `own`, deleting a task's own files or stopping its own processes isn't
    risky. What makes the whole line risky (overwriting a settings file, unbalanced quotes) comes as one ([], why)."""
    if _CLOBBER_DOTFILE.search(command):
        return [([], "overwrites a settings file")]
    if (pieces := _simple_commands(command)) is None:
        return [([], "can't be checked")]
    found, mine = [], None
    for argv in pieces:
        if not (why := _piece(argv)):
            continue
        if own and (mine := mine or _Mine(command, pieces, own)).covers(argv):
            continue
        found.append((_strip_prefixes(argv) or argv, why))
    return found


def _risk(command: str, own: "Own | None" = None) -> tuple[str, str]:
    """Why a command is risky, and the program (with its subcommand) that makes it so; ('', '') if it isn't.
    With `own`, deleting a task's own files or stopping its own processes isn't."""
    if not (found := risky_pieces(command, own)):
        return "", ""
    argv, why = found[0]
    if not argv:
        return why, "a redirect" if why == "overwrites a settings file" else "unbalanced quotes"
    return why, program(argv)


def program(argv: list[str]) -> str:
    """'brew install', 'git push', 'rm': the program a simple command runs, with its subcommand if it has one."""
    argv = _strip_prefixes(argv) or argv
    words = [Path(argv[0].lstrip("\\")).name]
    rest = _options_then(argv[1:], {"-C", "-c", "-R", "--repo"})
    if rest and re.fullmatch(r"[a-z][a-z-]{1,20}", rest[0]) and words[0] not in _ALWAYS:
        words.append(rest[0])
    return " ".join(words)


def risky(command: str, own: "Own | None" = None) -> str:
    """Why a command needs the user's yes before it runs ('deletes files', 'installs software', ...), or ''
    if it can simply run. Errs on the side of asking: anything it can't read counts as risky. For a background
    task, `own` is what's its own: deleting in its folders and stopping its processes needs no yes."""
    return _risk(command, own)[0]


def same_risk(a: str, b: str) -> bool:
    """Whether two command lines do the same risky things, in the same folders, so a yes to one is a yes to the other:
    only what wraps them may differ (a pipe into tail, 2>&1, a git status before). False for a line that isn't risky,
    or can't be checked."""
    def risk(command: str) -> tuple | None:
        found = risky_pieces(command)
        if not found or any(not argv for argv, _ in found) or (pieces := _simple_commands(command)) is None:
            return None
        folders = [p for p in map(_strip_prefixes, pieces) if p and p[0] in ("cd", "pushd", "popd", "chdir")]
        return found, folders

    return (mine := risk(a)) is not None and mine == risk(b)


def describe(command: str, own: "Own | None" = None) -> str:
    """A risky command's reason and program, taken from the command itself and never from what a model says
    about it: 'installs software (brew install)'. '' if it isn't risky."""
    why, what = _risk(command, own)
    return f"{why} ({what})" if why else ""


# Package managers that run some steps (builds, post-install scripts) in a macOS sandbox of their own, which can't start
# inside another one: Homebrew and MacPorts. A task's approved install with one runs outside the host profile.
_OWN_SANDBOX = {"brew", "port"}
_STDIN_FILTERS = {"head", "tail"}  # what may trim their output, when it reads no file


def own_sandbox(command: str) -> bool:
    """Whether a command line only installs or removes software with a package manager that sandboxes its own steps
    (brew install, port upgrade): such a line is always risky, and once approved it runs outside the host profile
    (jarvis.safety.host), whose walls would stop it. So nothing else may ride along: each simple command is one of
    those, run by its bare name with no prefix or variable before it, a cd, or a head or tail reading only what they
    print; no substitution, and no redirect but 2>&1."""
    if (_SUBSTITUTIONS.search(command) or re.search(r"[<>]", command.replace("2>&1", ""))
            or not (pieces := _simple_commands(command))):
        return False
    installs = False
    for argv in pieces:
        if argv[0] in _OWN_SANDBOX and _piece(argv) == "installs software":
            installs = True
        elif not (argv[0] == "cd" or argv[0] in _STDIN_FILTERS and all(a.startswith("-") or a.isdigit() for a in argv[1:])):
            return False
    return installs


# ── The user's own order ────────────────────────────────────────
# Words that plainly tell JARVIS to do the very thing a risky command does ("push it", "restart now", "merge and push",
# "delete the build folder", "install ffmpeg") are the user's go-ahead for that command, so it isn't asked about
# again: in the turn they're said, for the voice shell, and for a task, in the work they send it on. So is a clear
# yes to JARVIS's own question that asked to do just that ("Shall I push the changes?" "Yes."). Only these verbs
# count, each for the commands it plainly names: a delete must name every path, an install every package, a pkill
# its process. A question, a no, a hedge or a condition ("if", "don't", "before", "should") makes the words no order
# at all. Everything else that's risky (sending data off the Mac, system settings, code the check can't see into,
# discarding git work, secrets) has no order words and is still asked about.

_ORDER_MOST_WORDS = 40
_ORDER_NOT = set("""no nope nah not don't dont never wait hold hang cancel abort unless if but except later maybe
perhaps should shall whether might before after until once when first instead without rather tomorrow can't cannot
won't shouldn't isn't wouldn't didn't haven't""".split())
# In JARVIS's own question, a choice or a condition: a yes to it doesn't say which
_QUESTION_NOT = set("""or no not don't dont never without instead unless if before after until once first""".split())
_VERBS = {  # a word of the user's: what it orders
    "push": {"push"}, "restart": {"restart"}, "reload": {"restart"}, "relaunch": {"restart"}, "kickstart": {"restart"},
    "stop": {"stop"}, "kill": {"stop"}, "quit": {"stop"}, "close": {"stop"}, "terminate": {"stop"}, "shut": {"stop"},
    "start": {"start"}, "launch": {"start"}, "install": {"install"}, "reinstall": {"install"},
    "uninstall": {"uninstall"}, "remove": {"delete", "uninstall"}, "upgrade": {"upgrade"}, "update": {"upgrade"},
    "sync": {"sync"}, "delete": {"delete"}, "trash": {"delete"}, "erase": {"delete"}, "wipe": {"delete"},
    "merge": {"merge"}, "create": {"open"}, "open": {"open"}, "raise": {"open"},
}
_SERVICES = {  # what each order lets launchctl, brew services, docker and podman do, by subcommand
    "restart": {"kickstart", "kill", "bootout", "bootstrap", "load", "unload", "stop", "start", "restart"},
    "stop": {"bootout", "unload", "stop", "kill"},
    "start": {"bootstrap", "load", "start", "kickstart", "run"},
}
_PACKAGES = {  # what each order lets a package manager do, by subcommand
    "install": {"install", "i", "ci", "add", "reinstall", "sync"},
    "uninstall": {"uninstall", "remove", "rm", "un"},
    "upgrade": {"upgrade", "update", "up"},
    "sync": {"sync"},
}
_PULL_REQUEST = {"pr", "prs", "pull", "mr", "request"}
_PUSH_DELETES = {"-d", "--delete", "--prune", "--mirror"}  # pushes that delete on the remote: never ordered
_DELETERS = {"rm", "rmdir", "unlink", "trash"}
_CONTAINER_REMOVES = {"rm", "rmi", "prune", "down", "pause"}  # docker and podman: more than a restart or a stop


def _acts(words: list[str]) -> set[str]:
    """What these words order done, by their verbs: 'push', 'restart', 'delete', ..."""
    return {act for w in words for act in _VERBS.get(w, ())}


def _order_words(said: str) -> list[str] | None:
    """The words of an order, or None if they can't be one: a question, a no, a hedge or a condition."""
    if "?" in said:
        return None
    words = _words(said)
    if not words or len(words) > _ORDER_MOST_WORDS or _ORDER_NOT.intersection(words):
        return None
    return words


def orders(said: str) -> set[str]:
    """What the user's words order done ('push', 'restart', 'delete', ...), if they're an order at all; else {}."""
    return _acts(_order_words(said) or [])


def _named(word: str, said: set[str]) -> bool:
    """Whether the user's words name `word` (a package, a process, a file name): one of its words of 3 letters or
    more."""
    return any(len(w) >= 3 and w in said for w in re.findall(r"[a-z0-9]+", word.lower()))


def _path_named(word: str, said: set[str]) -> bool:
    """Whether the user's words name the file or folder `word`: its own name, never a home, a top folder or a word the
    shell could make into something else (a glob, a $VAR)."""
    if _NOT_LITERAL & set(word):
        return False
    path = Path(word).expanduser()
    if path.name in ("", ".", "..") or path == Path.home() or (path.is_absolute() and len(path.parts) <= 3):
        return False
    return _named(path.name, said)


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _push_as_said(rest: list[str], said: set[str]) -> bool:
    """A git push (its arguments after "push"): never one that deletes on the remote, and forced only if said."""
    if _PUSH_DELETES & set(rest) or any(a.startswith((":", "+:")) for a in rest):
        return False
    forced = any(a in ("-f", "--force") or a.startswith(("--force", "+")) for a in rest)
    return not forced or "force" in said


def _install_as_said(words: list[str], acts: set[str], said: set[str]) -> bool:
    """A package manager's command (from its subcommand on): what was ordered, for the packages named, or for none."""
    i = next((i for i, w in enumerate(words) if any(w in subs for subs in _PACKAGES.values())), None)
    if i is None:
        return False
    return (any(words[i] in _PACKAGES[act] for act in acts & _PACKAGES.keys())
            and all(_named(p, said) for p in _operands(words[i + 1:])))


def _ordered_piece(argv: list[str], why: str, acts: set[str], said: set[str]) -> bool:
    """Whether one risky piece of a command line does just what was ordered."""
    name, args = Path(argv[0].lstrip("\\")).name, argv[1:]
    words = _options_then(args, {"-C", "-c", "--git-dir", "--work-tree", "-m", "-R", "--repo"})  # subcommand on
    sub, rest = (words or [""])[0], words[1:]
    if name == "git" and sub == "push":
        return "push" in acts and _push_as_said(rest, said)
    if why == "deletes files":
        paths = _operands(args if name in _DELETERS else rest if (name, sub) == ("git", "rm") else [])
        return "delete" in acts and bool(paths) and all(_path_named(p, said) for p in paths)
    if why == "stops a process" and name in ("kill", "pkill", "killall"):
        targets = _operands(args)  # a process number for kill, which can't be named; a name for the others
        return "stop" in acts and (name == "kill" or bool(targets) and all(_named(t, said) for t in targets))
    if why in ("stops a process", "changes the system") and (name in ("launchctl", "docker", "podman")
                                                            or (name, sub) == ("brew", "services")):
        if name in ("docker", "podman") and set(args) & _CONTAINER_REMOVES:
            return False
        does = {"brew": rest[:1], "launchctl": [sub]}.get(name, args)
        return any(set(does) & _SERVICES[act] for act in acts & _SERVICES.keys())
    if why == "installs software":
        return _install_as_said(words, acts, said)
    if why.startswith("changes things on ") and sub in ("pr", "mr") and rest:
        return {"merge": "merge", "create": "open"}.get(rest[0]) in acts and bool(_PULL_REQUEST & said)
    return False


def _does_as_said(command: str, acts: set[str], said: set[str], own: "Own | None") -> bool:
    """Whether every risky piece of a command does just what `acts` order, with `said` the words that name things."""
    if not acts or names_secret(command) or reads_environment(command):
        return False
    found = risky_pieces(command, own)
    return bool(found) and all(argv and _ordered_piece(argv, why, acts, said) for argv, why in found)


def ordered(said: str, command: str, own: "Own | None" = None) -> bool:
    """Whether the user's own words plainly order what a risky command does, every risky piece of it: 'push it' for
    git push, 'restart now' for launchctl kickstart, 'delete the build folder' for rm -rf build, 'install ffmpeg' for
    brew install ffmpeg. Never a question, a no or a condition, and never a kind of command no words order."""
    return _does_as_said(command, orders(said), set(_words(said)), own)


def agreed_to(asked: str, said: str, command: str, own: "Own | None" = None, yes: bool | None = None) -> bool:
    """Whether the user's words are a clear yes to JARVIS's question just before them (the last sentence of `asked`,
    its reply), which asked to do just what the command does: "Shall I push the changes?" "Yes, go ahead." Never a
    question that offers a choice or a condition ("restart now, or wait for them to finish?"). `yes` says whether the
    words are a yes, when that's known otherwise (jarvis.safety.consent); else the word lists decide."""
    question = re.split(r"(?<=[.!?])\s+", asked.strip())[-1]
    if not question.endswith("?") or not (yes_scope(said) is not None if yes is None else yes):
        return False
    words = _words(question)
    if _QUESTION_NOT.intersection(words):
        return False
    return _does_as_said(command, _acts(words), set(words) | set(_words(said)), own)


# ── A task's own files and processes ────────────────────────────
# A background task may delete inside its own folders and stop the processes it started without a yes, but only
# where the command itself proves it: each path is spelled out (or a folder this command just made with mktemp),
# resolves, symlinks and all, strictly inside one of its folders, never into a .git; each process is one this
# command started ($!), or one the task's host commands started (Own.process). Anything else is asked about.

@dataclass(frozen=True)
class Own:
    """What's a background task's own: where its command starts, the folders it may delete in (its worktree or
    project folder, and its temp folder), and whether a process is one it started."""

    folder: Path
    roots: tuple[Path, ...]
    process: Callable[[int], bool] = lambda pid: False


# With any of these in the command, a word may not mean what it says (an alias, a glob option, a cd path), or a
# link or move may put something else where a path points: nothing in it is taken as the task's own
_UNSURE = re.compile(r"\b(alias|unalias|function|setopt|unsetopt|emulate|hash|enable|disable|CDPATH|IFS|GLOBIGNORE)\b"
                     r"|\(\s*\)|\bset\s+[-+]")
_UNSURE_PROGRAMS = {"ln", "mv", "xargs", "parallel"}  # the last two add arguments of their own
_NOT_LITERAL = set("$`{}[]\\*?")
_MKTEMP = r"\"?\$\(\s*mktemp((\s+-[dq]+)*(\s+-[dq]*t\s+[\w.-]+)?(\s+-[dq]+)*)\s*\)\"?"  # in $TMPDIR, made just now
_JOB = re.compile(r"%(\d+|%|\+|-)")
_DELETE_FLAGS = {"rm": set("dfiIPRrvWx"), "rmdir": {"v"}, "unlink": set()}  # the options each may take


class _Mine:
    """What one command line does to a task's own files and processes."""

    def __init__(self, command: str, pieces: list[list[str]], own: Own) -> None:
        self.own = own
        self.roots = [r.resolve() for r in own.roots]
        self.guarded = [*self.roots, ROOT.resolve()]  # never deleted whole, nor anything holding them
        words = {Path(w).name for argv in pieces for w in argv}  # as a program, or run by one (find -exec mv)
        self.sure = not _UNSURE.search(command) and not words & _UNSURE_PROGRAMS
        self.pids = self._reads(self._set_only(command, r"\$!"))  # $NAMEs only ever set to $!: processes it started
        self.temps = self._reads(self._set_only(command, _MKTEMP))  # those only ever set to a folder mktemp just made
        self.cwds = self._cwds(pieces)

    @staticmethod
    def _set_only(command: str, value: str) -> set[str]:
        """The shell variables that this command sets only ever to `value`, and otherwise just reads as $NAME."""
        names = set()
        for name in {m.group(1) for m in re.finditer(rf"(?<![\w$])([A-Za-z_]\w*)=(?:{value})(?=[\s;&|)]|$)", command)}:
            sets = len(re.findall(rf"(?<![\w$]){name}=(?:{value})(?=[\s;&|)]|$)", command))
            reads = len(re.findall(rf"\$(?:{name}\b(?![\[(:])|\{{{name}\}})", command))
            if sets + reads == len(re.findall(rf"\b{name}\b", command)):
                names.add(name)
        return names

    @staticmethod
    def _reads(names: set[str]) -> set[str]:
        """How the command reads these variables: $NAME or ${NAME}."""
        return {f"${n}" for n in names} | {f"${{{n}}}" for n in names}

    @staticmethod
    def _literal(word: str) -> Path | None:
        """A word as the path it names, if the shell leaves it as it is (but for a leading ~); else None."""
        if word == "~" or word.startswith("~/"):
            word = str(Path.home()) + word[1:]
        if not word or _NOT_LITERAL & set(word) or word[0] in "=~":
            return None
        return Path(word)

    def _cwds(self, pieces: list[list[str]]) -> set[Path] | None:
        """Every folder the command might be in when it runs a piece, or None if one can't be known."""
        start = self.own.folder.resolve()
        cds = [a for a in (_strip_prefixes(p) for p in pieces) if a and a[0] in ("cd", "pushd", "popd", "chdir")]
        found = {start}
        for argv in cds:
            if argv[0] == "popd":
                continue  # back where it was, which is counted already
            args = [a for a in argv[1:] if not re.fullmatch(r"-[LPqs]+", a)]
            if not args:
                found.add(Path.home().resolve())
                continue
            if len(args) > 1 or args[0][0] in "-+" or (target := self._literal(args[0])) is None:
                return None
            if not target.is_absolute():
                if len(cds) > 1:
                    return None  # which folder it's relative to depends on the order they run in
                target = start / target
            found.add(target.resolve())
        return found

    def _inside(self, path: Path, strictly: bool = True) -> bool:
        """Whether a resolved path is in one of the task's folders (strictly: not the folder itself), outside any
        .git, and holds none of the others, or JARVIS's own."""
        for root in self.roots:
            if path.is_relative_to(root) and ".git" not in path.relative_to(root).parts:
                return not any(g.is_relative_to(path) and (strictly or g != path) for g in self.guarded)
        return False

    def _where(self, path: Path) -> list[Path] | None:
        """Where a path from the command resolves, from each folder the command might be in."""
        if path.is_absolute():
            return [path.resolve()]
        return None if self.cwds is None else [(cwd / path).resolve() for cwd in self.cwds]

    def _owned_path(self, word: str) -> bool:
        if word in self.temps:
            return True
        head, tail = os.path.split(word.rstrip("/") if word.strip("/") else word)
        if set("*?") & set(tail) and not _NOT_LITERAL & set(tail.replace("*", "").replace("?", "")):
            # a glob, in the last part only: never one that could match .git, or a dotfile
            if "**" in tail or tail.startswith(".") or fnmatch.fnmatchcase(".git", tail):
                return False
            return self._owned_literal(head or ".", strictly=False)
        return self._owned_literal(word)

    def _owned_literal(self, word: str, strictly: bool = True) -> bool:
        path = self._literal(word)
        where = None if path is None else self._where(path)
        return bool(where) and all(self._inside(p, strictly) for p in where)

    def _owned_process(self, word: str) -> bool:
        if word in ("$!", "${!}") or _JOB.fullmatch(word):
            return True
        if word in self.pids:
            return True
        return word.isdigit() and self.own.process(int(word))

    def covers(self, argv: list[str]) -> bool:
        """Whether a risky simple command only deletes the task's own files, or stops its own processes."""
        if not self.sure or not (argv := _strip_prefixes(argv)):
            return False
        program, args = argv[0], argv[1:]
        if (name := program.removeprefix("/bin/")) in _DELETE_FLAGS:
            allowed, i = _DELETE_FLAGS[name], 0
            while i < len(args) and args[i].startswith("-") and args[i] != "-":
                if args[i] == "--":
                    i += 1
                    break
                if not set(args[i][1:]) <= allowed:
                    return False
                i += 1
            operands = args[i:]
            return bool(operands) and (name != "unlink" or len(operands) == 1) and all(map(self._owned_path, operands))
        if program in ("find", "/usr/bin/find") and "-delete" in args:
            i = 0
            while i < len(args) and args[i] in ("-E", "-P", "-X", "-d", "-s", "-x"):
                i += 1
            starts = []
            while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", "!", ")"):
                starts.append(args[i])
                i += 1
            links = {"-H", "-L", "-follow", "-exec", "-execdir", "-ok", "-okdir"}
            return bool(starts) and not links & set(args) and all(map(self._owned_literal, starts))
        if program in ("kill", "/bin/kill"):
            i = 0
            if args[:1] in (["-s"], ["-n"]):
                i = 2
            elif args and re.fullmatch(r"-(\d+|[A-Z]+|SIG[A-Z]+)", args[0]):
                i = 1
            if args[i : i + 1] == ["--"]:
                i += 1
            operands = args[i:]
            return bool(operands) and all(map(self._owned_process, operands))
        return False


# ── What a task may never run ───────────────────────────────────

# JARVIS's own controls: the runner's socket, and the endpoint that answers approvals
_CONTROLS = re.compile(r"runner\.sock|/api/approvals|/approvals/\d")
_KEYCHAIN_DUMPS = re.compile(r"find-(generic|internet)-password|dump-keychain|export|unlock-keychain|set-key-partition-list"
                             r"|add-(generic|internet)-password|delete-(generic|internet)-password")
_ROOT = {"sudo", "su", "doas"}
# In-place tasks share the user's own checkout: git commands that commit, push, move branches or drop changes there
IN_PLACE_GIT = ("commit", "push", "reset", "checkout", "switch", "restore", "stash", "clean", "rebase", "merge",
                "pull", "cherry-pick", "revert", "am", "branch", "tag", "worktree", "update-ref", "gc", "prune", "init")


def refused(command: str, in_place: bool = False) -> str:
    """Why a background task may never run a command on the Mac, even with the user's yes, or ''.
    `in_place`: the task works in the user's own checkout, where git is left to them."""
    if names_secret(command):
        return "it touches secrets, which stay out of reach even with a yes"
    if reads_environment(command):
        return "it reads other processes' environments, where their keys are, which stay out of reach even with a yes"
    if _CONTROLS.search(command):
        return "it touches JARVIS's own controls, which tasks can't reach"
    for argv in _simple_commands(command) or []:
        argv = _strip_prefixes(argv)
        if not argv:
            continue
        program, rest = Path(argv[0].lstrip("\\")).name, _options_then(argv[1:])
        if program in _ROOT:
            return f"{program} wants a password typed in a terminal, and tasks have none"
        if program == "security" and rest and _KEYCHAIN_DUMPS.fullmatch(rest[0]):
            return "it reads or changes passwords in the keychain"
        if in_place and program == "git":
            sub = _options_then(argv[1:], {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
            if sub and sub[0] in IN_PLACE_GIT:
                return (f"git {sub[0]} changes the user's own checkout, which in-place tasks leave to them; "
                        "list it in your summary instead")
    return ""
