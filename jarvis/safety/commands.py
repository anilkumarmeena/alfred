"""Shell-command risk rules and spoken yes/no parsing, shared by the voice shell and background tasks.

  risky / describe        why a command needs a yes ('deletes files'), or ''; `own` exempts a task's own files/pids
  refused                 why a task may never run it, even with a yes (secrets, JARVIS controls, sudo)
  own_sandbox             a bare brew/port install, approved to run outside the host profile
  names_secret / secret_path / reads_environment   secret detection, refused even with a yes
  approves / refuses / yes_scope                   clear yes / no / how far a yes goes, from word lists
  tells_approve / tells_deny / ordered / agreed_to the user's words as a later answer or a direct order
  same_risk               two lines do the same risky things in the same folders

The secret lists here are the single source for the shell deny rules, task file checks and the host profile.
"""

import fnmatch
import functools
import hashlib
import itertools
import os
import re
import shlex
import stat
import string
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.core import platform
from jarvis.core.config import CLAUDE_CONFIG_DIR, DATA_DIR, ROOT, SECRETS_DIR
from jarvis.safety import scripts
from jarvis.safety.scripts import UNSEEN

# ── Secrets ─────────────────────────────────────────────────────

# Home-folder logins, keys and keychains, macOS and Linux alike: a path that doesn't exist here costs nothing.
# Linux: GNOME Keyring, KWallet, pass, Bitwarden's vault and the NSS key store Chromium keeps client keys in.
HOME_SECRETS = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.docker/config.json",
                "~/Library/Keychains", "~/.git-credentials", "~/.npmrc", "~/.pypirc", "~/.local/share/keyrings",
                "~/.local/share/kwalletd", "~/.password-store", "~/.config/Bitwarden", "~/.pki/nssdb"]
# JARVIS's own: keys and sign-ins, Claude login (and voice logs), .env, and the browser profile (cookies = logins)
JARVIS_SECRETS = [SECRETS_DIR, Path(CLAUDE_CONFIG_DIR), ROOT / ".env", DATA_DIR / "browser/profile"]
# Secret wherever they appear
SECRET_DIRS = {"secrets", ".ssh", ".aws", ".gnupg", "Keychains"}
# .env.example and the like hold placeholders, not secrets: every check here leaves them out, while .env, .env.local,
# .env.production and any other .env.* stay secret. Neither Seatbelt's regexes nor globs can say "but not these", so
# the rest is spelled out from where a name leaves every template's path (_ENV_RESTS).
ENV_TEMPLATES = ("example", "sample", "template")
# What follows '.env.' in a name that isn't a template: (a prefix of one, characters that can't come next, whether the
# prefix alone counts). Any other character after a prefix, or a prefix that's no template itself, is secret.
_ENV_RESTS = [(p, "".join(sorted({w[len(p)] for w in ENV_TEMPLATES if len(w) > len(p) and w.startswith(p)})),
               p not in ENV_TEMPLATES)
              for p in sorted({w[:i] for w in ENV_TEMPLATES for i in range(len(w) + 1)})]
# Glob classes can't be negated (Claude Code's sandbox escapes ^ in them): file-name characters, spelled out
_NAME_CHARS = set(string.ascii_letters + string.digits + "._~+@")


def secret_file_names(char: str = ".", pem: bool = True) -> str:
    """Secret file names as one regex alternation, env templates left out.

    `char` is what a wildcard matches ('[^/]' stays within one path component). `pem=False` omits .pem files.
    """
    slash = "" if char == "." else "/"
    rests = "|".join(re.escape(p) + (f"([^{after}{slash}]{char}*)" if after else f"({char}+)") + "?" * alone
                     for p, after, alone in _ENV_RESTS)
    keys = "pem|key" if pem else "key"
    return (rf"\.env(\.({rests}))?|{char}+\.({keys}|p8|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)|\.netrc"
            rf"|\.npmrc|\.pypirc|\.git-credentials|credentials(\.json)?|{char}*client_secret{char}*\.json|tokens?\.json")


def _glob_class(but: str) -> str:
    """Glob class of the file-name characters except `but`, with runs as ranges and '-' first (so it's literal)."""
    chars = sorted(_NAME_CHARS - set(but))
    runs = [[c for _, c in run] for _, run in itertools.groupby(enumerate(chars), lambda ic: ord(ic[1]) - ic[0])]
    return "[-" + "".join("".join(r) if len(r) < 3 else f"{r[0]}-{r[-1]}" for r in runs) + "]"


SECRET_FILES = re.compile(secret_file_names(), re.IGNORECASE)
# Same files as globs, for Claude Code's Read deny rules
SECRET_PATTERNS = ["**/.env",
                   *(g for p, after, alone in _ENV_RESTS
                     for g in [f"**/.env.{p}"] * alone + [f"**/.env.{p}{_glob_class(after)}*"]),
                   "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.pfx", "**/id_rsa",
                   "**/id_dsa", "**/id_ecdsa", "**/id_ed25519", "**/.netrc", "**/.npmrc", "**/.pypirc",
                   "**/.git-credentials", "**/credentials", "**/credentials.json", "**/*client_secret*.json",
                   "**/token.json", "**/tokens.json"]

# Naming one of these refuses a command even with a yes: outside a sandbox no deny rule covers it
_SECRET_NAMES = re.compile(
    "|".join(re.escape(str(p)) for p in JARVIS_SECRETS)
    + "|" + "|".join(re.escape(p.removeprefix("~/")) for p in HOME_SECRETS)
    + r"|\.env(?!\w)|secrets/|\bid_(rsa|dsa|ecdsa|ed25519)\b(?!\.pub)|\.(pem|key|p8|p12|pfx)\b|client_secret"
    + r"|JARVIS_MASTER_KEY"
)
# A template's whole name, blanked before the search so neither .env nor ROOT/.env matches inside it
_ENV_TEMPLATE_NAMES = re.compile(rf"(?<![\w.-])\.env\.({'|'.join(ENV_TEMPLATES)})(?![\w.-])")


def names_secret(text: str) -> bool:
    """Whether text names a secret: a secrets folder, .env file (not a template), key or login."""
    return bool(_SECRET_NAMES.search(_ENV_TEMPLATE_NAMES.sub(" ", text)))


# Other processes' environments hold keys (MCP servers get tokens there). On macOS only setuid /bin/ps can read
# them; Linux exposes /proc/<pid>/environ.
_ENV_TEXT = re.compile(r"/proc/[^\s/]+/environ|\bprocargs|\blaunchctl\s+procinfo\b")
_PS_VALUES = set("oOpgGtuUMN")  # ps options taking a value; later letters in the cluster aren't flags


def _ps_shows_env(args: list[str]) -> bool:
    dashed = {"e", "E"} if platform.MAC else {"E"}  # Linux's ps -e means every process, as in ps -ef
    for n, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--"):
            letters = re.split(f"[{''.join(_PS_VALUES)}]", arg[1:], maxsplit=1)[0]
            if set(letters) & dashed:
                return True
        elif n == 0 and re.fullmatch(r"[A-Za-z]+", arg) and "e" in arg:  # BSD style: ps eww, ps auxe
            return True
    return False


def reads_environment(command: str) -> bool:
    """Whether a command reads other processes' environments (ps eww, /proc/<pid>/environ). Refused like secrets."""
    if _ENV_TEXT.search(command):
        return True
    for argv in _simple_commands(command) or []:
        argv = _strip_prefixes(argv)
        while argv and Path(argv[0]).name in _ROOT:
            argv = _strip_prefixes(_options_then(argv[1:], {"-u", "-g", "-C", "-h", "-p", "-U", "--user"}))
        if argv and Path(argv[0].lstrip("\\")).name == "ps" and _ps_shows_env(argv[1:]):
            return True
        if argv and Path(argv[0]).name in ("lldb", "gdb", "strace", "ltrace") and any(
                a in ("-p", "--attach-pid", "--pid") or a.startswith("--attach") for a in argv[1:]):
            return True
    return False


def secret_path(path: Path) -> bool:
    """Whether a resolved path is a secret: in a secrets/login folder, a .env file, key or credential."""
    if any(path.is_relative_to(Path(d).expanduser().resolve()) for d in (*JARVIS_SECRETS, *HOME_SECRETS)):
        return True
    return bool(SECRET_DIRS.intersection(path.parts)) or bool(SECRET_FILES.fullmatch(path.name))


# ── What counts as a yes, or a no ───────────────────────────────

# A yes only if, minus filler, it consists entirely of these phrases. Fast path only: anything else that isn't a
# clear no goes to the model (jarvis.safety.consent).
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
# Remarks that react rather than answer: a yes beside an answer ("great, go ahead"), never alone ("JARVIS, good",
# "Right." as in "right, hmm"); "that's right" and "all right" are phrases of their own
_REACTIONS = {(w,) for w in "good great cool perfect lovely brilliant excellent right".split()}
_ACTION = re.compile(  # "yes, merge it" counts as "go"
    r"\b(let's |let it |you can )?(run|merge|do|install|execute|apply|start|restart|launch|open|try|send|fetch|pull|push"
    r"|update|upgrade|kill|commit|deploy|build|delete|remove|stop|allow|render|close|quit|clean|clear|end|wipe|trash|shut)"
    r" (it|them|that|those|this|these|both|all)( up| down| off)?\b"
)
# "let's close the hidden Chrome": same, naming the object in a few words. "for", "from", "my" etc. make it a new
# request instead.
_ACTION_ON = re.compile(
    r"\b(let's |let it |you can )?(delete|remove|kill|stop|close|quit|clean up|clear|wipe|trash|shut down|install|push"
    r"|merge|run|restart|end) the( (?!(for|from|to|on|in|at|with|my|me|your|about|of|and|then|instead)\b)[a-z']+){1,3}\b"
)
# Contain "no"/"not" but mean yes
_YES_IDIOMS = re.compile(r"\b(why not|no problem|not a problem|no worries|no objection|no objections)\b")
# Hyphenated speech-recognition sounds: "mm-hmm"/"uh-huh" yes, "uh-uh"/"mm-mm" no
_YES_SOUNDS = re.compile(r"\b(m+-?h+m+|uh-huh)\b")
_NO_SOUNDS = re.compile(r"\b(uh-uh|mm+-mm+|nuh-uh|unh-unh)\b")
_FILLER = set("um umm uh uhh uhm er erm ah oh hmm mm well so and then now just sir jarvis please thanks thank you".split())
_REFUSAL = set("""no nope nah not don't dont never stop wait hold hang cancel abort forget skip leave later instead
rather but except only if unless actually first before why what how when where who which isn't shouldn't won't
can't cannot""".split())
# A clear no: opens with one of these (after filler), or is short and contains one
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
    """Whether a transcribed reply is a clear yes ("mm-hmm", "yeah, let's close the hidden Chrome").

    Tolerates punctuation, filler and "O.K."; anything less than a clear yes, a lone "thank you" or a remark alone
    ("good", "perfect, thanks") is not one.
    """
    if "?" in said:
        return False
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    if not words or len(words) > 12 or _REFUSAL.intersection(words):
        return False
    words = _ACTION_ON.sub("go", _ACTION.sub("go", " ".join(words))).split()
    agreed, i = False, 0
    while i < len(words):
        if phrase := next((p for p in _AFFIRMATIVE if tuple(words[i : i + len(p)]) == p), None):
            agreed = agreed or phrase not in _REACTIONS
            i += len(phrase)
        elif words[i] in _FILLER:
            i += 1
        else:
            return False
    return agreed


def refuses(said: str) -> bool:
    """Whether a reply is clearly a no. "Wait", a question or a change of subject isn't one."""
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    if len(words) <= 4 and _NO_WORDS.intersection(words):
        return True
    while words and words[0] in _NO_LEAD:
        words = words[1:]
    return any(tuple(words[: len(p)]) == p for p in _NO_OPENERS)


# ── Telling JARVIS to answer a task's request ───────────────────

# Said later, not right after the question, so it may name its object ("let it close the hidden Chrome"):
# looser than approves/refuses but still a short, plain instruction.
_TELL_MOST_WORDS = 20
_TELL_APPROVE = re.compile(
    r"\b(approve[ds]?|allow|permit|authori[sz]e|let (it|them|that|task|the task)|go ahead|go for it|green light"
    r"|ok it|okay it|yes|yeah|yep|sure|run it|do it|proceed|carry on)\b"
)
_TELL_DENY = re.compile(
    r"\b(deny|denied|decline|refuse|reject|block it|say no|tell it no|(don't|dont|do not) (let|allow|approve|run|do)"
    r"|no|nope|nah|not allowed|cancel)\b"
)
# Any of these (a no, condition, doubt, "wait") disqualifies a plain yes
_TELL_NOT_YES = set("""no nope nah not don't dont never wait hold hang cancel abort unless if but except deny denied
decline refuse reject can't cannot won't shouldn't isn't later maybe perhaps""".split())


def _told(said: str) -> list[str]:
    """Words of a short non-question instruction, yes-idioms normalised; else []."""
    words = _YES_IDIOMS.sub("yes", " ".join(_words(said))).split()
    return [] if "?" in said or len(words) > _TELL_MOST_WORDS else words


def tells_approve(said: str) -> bool:
    """Whether the words plainly approve a waiting command ('approve it', 'let it close the hidden Chrome').

    Never a question, a no, an "if" or a "wait".
    """
    if "?" in said:
        return False
    found, rest = _scoped(said)
    words = _told(rest)
    if not words and found:  # "always allow it": the scope phrase says it
        words = _told(said)
        return not _TELL_NOT_YES.intersection(words) and bool(_ALWAYS_ALLOW.search(" ".join(words)))
    return bool(words) and not _TELL_NOT_YES.intersection(words) and bool(_TELL_APPROVE.search(" ".join(words)))


# ── How long a yes lasts ────────────────────────────────────────
# once / session / always. Some phrases are a yes alone ("always"); others only beside one ("yes, don't ask again"),
# since "don't ask me again" alone might be a no.

SCOPES = ("once", "session", "always")  # narrowest first
_SCOPE_PHRASES = [  # (scope, is a yes alone, regex)
    ("once", True, r"(just |only )?(this|that) (once|one time|time)|(just |only )?once"),
    ("session", True, r"(for )?(the rest of )?(this|the) (session|conversation|call|task)"),
    ("always", True, r"always( (allow|approve|let) (it|that|them|this))?( for (this|that) kind of (command|commands|thing))?"),
    ("always", False, r"from now on|every time|for good|permanently|(and )?(don't|dont|do not|never) ask( me)?( about)?"
                      r"( (it|that|this|them))? again|(for )?(this|that) kind of (command|commands|thing)"),
]
_SCOPE_RE = [(scope, alone, re.compile(rf"\b({phrase})\b")) for scope, alone, phrase in _SCOPE_PHRASES]
_ALWAYS_ALLOW = re.compile(r"\balways (allow|approve|let) (it|that|them|this)\b")
_BROAD = re.compile(r"\b(kind|kinds|any|anything|whatever)\b")  # "always, any brew install": the whole kind


def _scoped(said: str) -> tuple[list[tuple[str, bool]], str]:
    """([(scope, is a yes alone)], the words with scope phrases removed)."""
    text, found = " ".join(_words(said)), []
    for scope, alone, phrase in _SCOPE_RE:
        if phrase.search(text):
            found.append((scope, alone))
            text = phrase.sub(" ", text)
    return found, " ".join(text.split())


def yes_scope(said: str) -> str | None:
    """For a clear yes: 'once' (default, or on conflict), 'session' or 'always'; None if not a clear yes."""
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
    """Scope from scope phrases alone; 'once' if none."""
    return _narrowest(_scoped(said)[0])


def scopes_said(said: str) -> list[str]:
    """Every scope named by a scope phrase."""
    return [scope for scope, _ in _scoped(said)[0]]


def _narrowest(found: list[tuple[str, bool]]) -> str:
    """Narrowest scope found, or 'once'."""
    return min((scope for scope, _ in found), key=SCOPES.index, default="once")


def said_breadth(said: str) -> str:
    """For an "always": 'kind' if the words cover any command of that kind, else 'exact'."""
    return "kind" if _BROAD.search(" ".join(_words(said))) else "exact"


def tells_deny(said: str) -> bool:
    """Whether the words plainly deny a waiting command ('deny it', 'no, don't let it'). Never a question."""
    words = _told(said)
    return bool(words) and (refuses(said) or bool(_TELL_DENY.search(" ".join(words))))


# ── What counts as risky ────────────────────────────────────────

# A line is split into simple commands at ; & | && || ( ) and newlines, each judged by program and args. Shell code
# a command runs (bash -c, a heredoc fed to a shell) is split the same way. Scripts and inline code are read and
# judged by what they do (`_code`); code the check can't read (eval, a download piped to a shell) is risky itself.
_PREFIXES = {"nohup", "time", "exec", "command", "builtin", "env", "nice", "caffeinate", "xargs", "timeout", "gtimeout",
             "stdbuf", "then", "do", "else", "elif", "if", "while", "until", "!", "noglob", "{", "}"}
_PREFIX_VALUES = {"-I", "-J", "-L", "-n", "-P", "-R", "-s", "-E", "-d", "-u", "-t", "-w"}  # xargs -I {}, nice -n 5
_SUBSTITUTIONS = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_FD_NUMBER = re.compile(r"(?<![^\s;&|()])\d+(?=[<>])")
_ALWAYS = {
    "rm": "deletes files", "rmdir": "deletes files", "unlink": "deletes files", "shred": "deletes files",
    "srm": "deletes files", "trash": "deletes files", "truncate": "deletes files", "dd": "writes a disk",
    "kill": "stops a process", "killall": "stops a process", "pkill": "stops a process",
    "shutdown": "stops the computer", "reboot": "stops the computer", "halt": "stops the computer",
    "sudo": "runs as root", "su": "runs as root", "doas": "runs as root",
    "chmod": "changes permissions", "chown": "changes permissions", "chgrp": "changes permissions",
    "chflags": "changes permissions", "crontab": "changes the system", "installer": "installs software",
    "softwareupdate": "installs software", "pipx": "installs software", "mas": "installs software",
    "npx": "installs software", "pnpx": "installs software", "bunx": "installs software", "uvx": "installs software",
    "port": "installs software",
    **dict.fromkeys(("scp", "sftp", "ssh", "ftp", "nc", "ncat", "netcat", "telnet", "mail", "sendmail"),
                    "sends data off the computer"),
    "csrutil": "changes the system",
    "spctl": "changes the system", "tccutil": "changes the system", "dscl": "changes the system",
    "tmutil": "changes the system", "osascript": "controls apps",
    "eval": UNSEEN, "watch": UNSEEN, "script": UNSEEN, "parallel": UNSEEN,
    "at": "changes the system", "batch": "changes the system",  # run commands later, as crontab does
    # Linux
    "poweroff": "stops the computer", "pkexec": "runs as root", "run0": "runs as root",
    "chattr": "changes permissions", "setfacl": "changes permissions", "setcap": "changes permissions",
    "passwd": "changes the system", "chpasswd": "changes the system", "chage": "changes the system",
    "chsh": "changes the system", "chfn": "changes the system", "usermod": "changes the system",
    "useradd": "changes the system", "userdel": "changes the system", "groupadd": "changes the system",
    "groupdel": "changes the system", "groupmod": "changes the system", "gpasswd": "changes the system",
    "visudo": "changes the system", "umount": "changes the system", "swapon": "changes the system",
    "swapoff": "changes the system", "modprobe": "changes the system", "insmod": "changes the system",
    "rmmod": "changes the system", "systemd-run": "changes the system", "dpkg-reconfigure": "changes the system",
    "update-rc.d": "changes the system", "chkconfig": "changes the system", "iptables-restore": "changes the system",
    "ip6tables-restore": "changes the system", "add-apt-repository": "installs software", "apt-key": "installs software",
    "fdisk": "writes a disk", "sfdisk": "writes a disk", "parted": "writes a disk", "wipefs": "writes a disk",
    "mkswap": "writes a disk", "cryptsetup": "writes a disk",
    "dbus-send": "controls apps", "qdbus": "controls apps", "xdotool": "controls apps", "ydotool": "controls apps",
    # Secret Service, KWallet and pass: like `security find-generic-password` (refused to tasks)
    "secret-tool": "reads the keychain", "kwallet-query": "reads the keychain", "pass": "reads the keychain",
}
_READ_ONLY_SUBCOMMANDS = {  # any other subcommand changes things
    "launchctl": {"list", "print", "print-disabled", "blame", "managername", "version", "getenv", "help"},
    "diskutil": {"list", "info", "activity", "listFilesystems"},
    "defaults": {"read", "read-type", "domains", "find", "help"},
    # other security subcommands read the keychain
    "security": {"find-identity", "cms"},
    # Linux
    "systemctl": {"status", "show", "cat", "help", "list-units", "list-unit-files", "list-sockets", "list-timers",
                  "list-jobs", "list-dependencies", "list-machines", "list-paths", "list-automounts", "is-active",
                  "is-enabled", "is-failed", "is-system-running", "show-environment", "get-default"},
    "loginctl": {"list-sessions", "session-status", "show-session", "list-users", "user-status", "show-user",
                 "list-seats", "seat-status", "show-seat", "help"},
    "timedatectl": {"status", "show", "show-timesync", "timesync-status", "list-timezones", "help"},
    "hostnamectl": {"status", "show", "help"},
    "localectl": {"status", "list-locales", "list-keymaps", "list-x11-keymap-models", "list-x11-keymap-layouts",
                  "list-x11-keymap-variants", "list-x11-keymap-options", "help"},
    "gsettings": {"get", "list-schemas", "list-relocatable-schemas", "list-keys", "list-children", "list-recursively",
                  "range", "describe", "writable", "monitor", "help"},
    "dconf": {"read", "list", "dump", "watch", "help"},
    "xdg-settings": {"get", "check"},
    "xdg-mime": {"query"},
    "busctl": {"list", "status", "monitor", "capture", "tree", "introspect", "get-property", "help"},
    "gdbus": {"introspect", "monitor", "help"},
    "ufw": {"status", "show", "version", "help"},
    "apt": {"list", "search", "show", "showsrc", "policy", "depends", "rdepends", "changelog", "help"},
    "apt-get": {"check", "changelog", "help"},
    **dict.fromkeys(("dnf", "dnf5", "yum", "microdnf"), frozenset({
        "list", "info", "search", "provides", "whatprovides", "repolist", "repoinfo", "repoquery", "check-update",
        "deplist", "updateinfo", "help"})),
    "zypper": {"search", "se", "info", "if", "list-updates", "lu", "list-patches", "lp", "packages", "pa", "patterns",
               "pt", "products", "pd", "repos", "lr", "services", "ls", "what-provides", "wp", "locks", "ll",
               "patch-check", "pchk", "help"},
    "apk": {"info", "search", "list", "policy", "dot", "stats", "version", "audit", "help"},
    "snap": {"list", "find", "info", "version", "connections", "interfaces", "services", "logs", "changes", "change",
             "tasks", "warnings", "aliases", "known", "model", "get", "run", "whoami", "help"},
    "flatpak": {"list", "info", "search", "remotes", "remote-ls", "remote-info", "history", "ps", "documents",
                "document-info", "permissions", "permission-show", "run", "help"},
}
_READ_ONLY_WHY = {"security": "reads the keychain", "busctl": "controls apps", "gdbus": "controls apps",
                  **dict.fromkeys(("apt", "apt-get", "dnf", "dnf5", "yum", "microdnf", "zypper", "apk", "snap", "flatpak"),
                                  "installs software")}
_BARE_READS = {"systemctl", "loginctl", "timedatectl", "hostnamectl", "localectl", "busctl"}  # alone: a status listing
_SUB_WHY = {  # subcommands with a reason of their own
    "systemctl": dict.fromkeys(("poweroff", "reboot", "halt", "kexec", "soft-reboot", "suspend", "hibernate", "sleep",
                                "hybrid-sleep", "suspend-then-hibernate", "rescue", "emergency"),
                               "stops the computer"),
    "snap": {"start": "stops a process", "stop": "stops a process", "restart": "stops a process",
             "connect": "changes permissions", "disconnect": "changes permissions"},
    "flatpak": {"kill": "stops a process", "override": "changes permissions", "permission-set": "changes permissions",
                "permission-remove": "changes permissions", "permission-reset": "changes permissions"},
}
_SUB_VALUES = {  # options taking a value, before the subcommand
    "systemctl": {"-t", "--type", "-p", "--property", "-P", "-H", "--host", "-M", "--machine", "-n", "--lines", "-o",
                  "--output", "-s", "--signal", "--state", "--root", "--image", "--kill-whom", "--kill-value",
                  "--job-mode", "--what", "--when", "--timestamp", "--preset-mode", "--boot-loader-entry",
                  "--reboot-argument"},
    "loginctl": {"-p", "--property", "-P", "-H", "--host", "-M", "--machine", "-n", "--lines", "-o", "--output", "-s",
                 "--signal", "--kill-whom"},
    "timedatectl": {"-H", "--host", "-M", "--machine", "-p", "--property"},
    "hostnamectl": {"-H", "--host", "-M", "--machine"},
    "localectl": {"-H", "--host", "-M", "--machine"},
    "busctl": {"-H", "--host", "-M", "--machine", "--match", "--size", "--destination"},
    "gsettings": {"--schemadir"},
    "apt": {"-o", "--option", "-c", "--config-file", "-t", "--target-release"},
    "apt-get": {"-o", "--option", "-c", "--config-file", "-t", "--target-release"},
    **dict.fromkeys(("dnf", "dnf5", "yum", "microdnf"), frozenset({
        "-c", "--config", "--installroot", "--releasever", "--repo", "--repoid", "--enablerepo", "--disablerepo", "-x",
        "--exclude", "--setopt", "-d", "--debuglevel", "-e", "--errorlevel", "--forcearch", "--color"})),
    "zypper": {"-R", "--root", "-c", "--config", "-D", "--reposd-path", "-C", "--cache-dir", "--raw-cache-dir",
               "--solv-cache-dir", "--pkg-cache-dir", "--userdata"},
    "apk": {"-X", "--repository", "-p", "--root", "--arch", "--cache-dir", "--keys-dir", "--repositories-file"},
}
_HOSTNAME_PARTS = {"hostname", "icon-name", "chassis", "deployment", "location"}  # hostnamectl X shows, X VALUE sets
# What these do is in their options, not a subcommand
_OPTION_DRIVEN = {"pacman", "yay", "paru", "rpm", "dpkg", "update-alternatives", "alternatives"}
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
# gh's plus glab-specific verbs
_GLAB_WRITES = _GH_WRITES | {"update", "approve", "revoke", "note", "rebase", "subscribe", "unsubscribe", "retry", "run",
                             "trigger", "mirror", "todo", "publish", "star", "unstar"}
# Config sections that can make git run a program later (! aliases, core.pager, hooks, filters, drivers, helpers)
_GIT_CODE_CONFIG = {"alias", "core", "include", "includeif", "filter", "diff", "difftool", "merge", "mergetool",
                    "credential", "gpg", "sequence", "pager", "interactive", "uploadpack", "receive"}
_GIT_CONFIG_VALUES = {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value", "--url"}
_GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool", "-l",
                     "--list"}
_GIT_CONFIG_WRITES = {"--add", "--replace-all", "--unset", "--unset-all", "--rename-section", "--remove-section"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_JS = {"node", "nodejs", "bun", "deno", "tsx", "ts-node"}
# Interpreters whose code the check doesn't read: whatever they're given to run is unseen
_OPAQUE = {"perl", "ruby", "php", "lua", "luajit", "Rscript", "R", "julia", "tclsh", "wish", "expect", "groovy",
           "escript", "pwsh", "powershell", "runghc", "guile", "racket"}
_INFO = {"-v", "-V", "-VV", "--version", "-version", "-h", "--help"}
_CLOBBER_DOTFILE = re.compile(r"(?<![0-9&])>>?\s*(?!/dev/)(\S*/)?\.[A-Za-z]")  # > ~/.zshrc, >> .env
# devicectl/simctl subcommands that are risky (installing a build is not)
_DEVICE_CHANGES = {"uninstall", "terminate", "reboot", "shutdown", "unpair", "erase", "signal", "delete"}
_XCRUN_VALUES = {"--sdk", "-sdk", "--toolchain", "-toolchain"}


def _options_then(argv: list[str], takes_value: set[str] = frozenset()) -> list[str]:
    """argv minus leading options (and the values of those in `takes_value`)."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in takes_value else 1
    return argv[i:]


def _git_config(args: list[str]) -> str:
    """Risk of `git config`: opening an editor, or writing config that runs code."""
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
    # -c core.pager=..., -c alias.x='!cmd': command-line config can run anything
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
    """Risk of a gh/glab command: writes to `host`, or touches the CLI's credentials."""
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
        return "sends data off the computer"
    method = next((b for a, b in zip(args, args[1:]) if a in ("-X", "--request")), "") or next(
        (a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")
    return "sends data off the computer" if method.upper() not in ("", "GET", "HEAD") else ""


# ── Linux tools whose risk is in their options ──


def _prefixed(word: str, choices: tuple[str, ...]) -> list[str]:
    """Choices `word` abbreviates (nmcli and ip accept any unambiguous prefix)."""
    return [c for c in choices if c.startswith(word)]


def _reads_only(word: str, reads: tuple[str, ...], writes: tuple[str, ...]) -> bool:
    """Whether `word` can only abbreviate a read."""
    found = _prefixed(word, reads + writes)
    return bool(found) and all(c in reads for c in found)


_NM_OBJECTS = ("general", "networking", "radio", "connection", "device", "agent", "monitor", "help")
_NM_VERBS = {  # (reads, writes)
    "connection": (("show", "monitor"), ("up", "down", "add", "modify", "clone", "edit", "delete", "reload", "load",
                                         "import", "export", "migrate")),
    "device": (("status", "show", "monitor", "lldp"), ("set", "up", "connect", "reapply", "modify", "down",
                                                      "disconnect", "delete", "wifi")),
    "general": (("status", "permissions"), ("hostname", "logging", "reload")),
}
_NM_VALUES = {"-f", "--fields", "-g", "--get-values", "-m", "--mode", "-c", "--colors", "-e", "--escape", "-w", "--wait"}


def _nmcli(args: list[str]) -> str:
    if any(a.startswith("-") and a.lstrip("-") and "show-secrets".startswith(a.lstrip("-")) or a == "show-password"
           for a in args):
        return "reads the keychain"  # Wi-Fi and VPN passwords
    words = _options_then(args, _NM_VALUES)
    if not words:
        return ""
    objects = _prefixed(words[0], _NM_OBJECTS)
    if len(objects) != 1:
        return "changes the system"
    obj, rest = objects[0], words[1:]
    if obj in ("monitor", "help"):
        return ""
    if obj in ("radio", "networking"):
        return "changes the system" if {"on", "off"} & set(rest) else ""
    if obj not in _NM_VERBS:
        return "changes the system"  # agent: answers password prompts
    if not rest:
        return ""
    reads, writes = _NM_VERBS[obj]
    if obj == "general" and len(rest) == 1 and _prefixed(rest[0], reads + writes) in (["hostname"], ["logging"]):
        return ""  # shown, not set
    if obj == "device" and _prefixed(rest[0], reads + writes) == ["wifi"]:
        return "" if len(rest) == 1 or _prefixed(rest[1], ("list", "connect", "hotspot", "rescan")) == ["list"] else \
            "changes the system"
    return "" if _reads_only(rest[0], reads, writes) else "changes the system"


_IP_READS = ("show", "list", "lst", "get", "help")
_IP_WRITES = ("add", "append", "change", "chg", "del", "delete", "exec", "flush", "prepend", "replace", "restore",
              "save", "set", "attach", "detach", "update", "test")


def _ip(args: list[str]) -> str:
    if any(a.startswith("-") and len(a) > 1 and "batch".startswith(a.lstrip("-")) for a in args):
        return "changes the system"  # -batch FILE runs its commands
    words = _options_then(args, {"-n", "-netns", "-f", "-family", "-l", "-loops", "-rc", "-rcvbuf"})
    if not words or len(words[0]) >= 3 and "monitor".startswith(words[0]) or len(words) < 2:
        return ""  # ip addr: a listing
    return "" if _reads_only(words[1], _IP_READS, _IP_WRITES) else "changes the system"


_XT_WRITES = set("ADIRFZNXPE")  # iptables commands that change rules
_XT_LONG_WRITES = {"--append", "--delete", "--insert", "--replace", "--flush", "--zero", "--new-chain", "--delete-chain",
                   "--policy", "--rename-chain"}


def _iptables(args: list[str]) -> str:
    letters = {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}
    longs = {a.split("=")[0] for a in args if a.startswith("--")}
    if letters & _XT_WRITES or longs & _XT_LONG_WRITES:
        return "changes the system"
    reads = letters & set("LSChV") or longs & {"--list", "--list-rules", "--check", "--help", "--version"}
    return "" if reads else "changes the system"


def _nft(args: list[str]) -> str:
    if any(a in ("-f", "--file", "-i", "--interactive") or a.startswith("--file=") for a in args):
        return "changes the system"
    words = _options_then(args, {"-I", "--includepath", "-D", "--define"})
    return "" if not words or words[0].split()[0] in ("list", "describe", "monitor", "help") else "changes the system"


_FIREWALLD_READS = ("--state", "--get-", "--list-", "--query-", "--info-", "--check-config", "--zone", "--permanent",
                    "--help", "--version", "-h", "-V", "-q", "--quiet")


def _firewall_cmd(args: list[str]) -> str:
    return "" if args and all(a.startswith(_FIREWALLD_READS) for a in args) else "changes the system"


_PACMAN_OPS = {"--sync": "S", "--remove": "R", "--upgrade": "U", "--database": "D", "--query": "Q", "--files": "F",
               "--deptest": "T", "--version": "V", "--help": "h"}
_PACMAN_SYNC = {"--search": "s", "--info": "i", "--list": "l", "--groups": "g", "--print": "p", "--refresh": "y",
                "--sysupgrade": "u", "--downloadonly": "w", "--clean": "c"}


def _pacman(args: list[str]) -> str:
    """pacman, and the AUR helpers yay and paru that share its options."""
    op, letters = "", set()
    for a in args:
        if a.startswith("--"):
            op = op or _PACMAN_OPS.get(a, "")
            letters.add(_PACMAN_SYNC.get(a, ""))
        elif a.startswith("-"):
            for c in a[1:]:
                if c in "DFQRSTUVh" and not op:
                    op = c
                else:
                    letters.add(c)
    if op in ("Q", "T", "V", "h") or op == "F" and "y" not in letters:
        return ""
    if op == "S" and letters & set("silgp") and not letters & set("yuwc"):
        return ""  # search, info, list, groups, print: nothing installed
    return "installs software"


def _dpkg(args: list[str]) -> str:
    longs = ("--install", "--remove", "--purge", "--unpack", "--configure", "--triggers-only", "--set-selections",
             "--clear-selections", "--add-architecture", "--remove-architecture", "--update-avail", "--merge-avail",
             "--clear-avail", "--record-avail")
    letters = {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}
    return "installs software" if letters & set("irPA") or any(a.startswith(longs) for a in args) else ""


def _rpm(args: list[str]) -> str:
    longs = ("--install", "--upgrade", "--freshen", "--erase", "--reinstall", "--import", "--rebuilddb", "--initdb",
             "--restore", "--setperms", "--setugids")
    letters = {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}
    if any(a.startswith(longs) for a in args) or letters & set("iUFe") and not letters & set("qVK"):
        return "installs software"  # rpm -qi is a query
    return ""


def _alternatives(args: list[str]) -> str:
    writes = {"--install", "--remove", "--remove-all", "--auto", "--set", "--config", "--all", "--set-selections"}
    reads = {"--display", "--query", "--list", "--get-selections", "--help", "--version"}
    return "" if reads & set(args) and not writes & set(args) else "changes the system"


def _journalctl(args: list[str]) -> str:
    if any(a.startswith("--vacuum") for a in args):
        return "deletes files"
    changes = {"--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys",
               "--update-catalog"}
    return "changes the system" if any(a.split("=")[0] in changes for a in args) else ""  # reading logs is fine


def _sysctl(args: list[str]) -> str:
    """sysctl sets a value with -w or name=value (Linux and macOS); -p/--system load settings files."""
    letters = {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}
    if letters & {"w", "p"} or any(a.split("=")[0] in ("--write", "--load", "--system") for a in args) or any(
            "=" in a for a in args if not a.startswith("-")):
        return "changes the system"
    return ""


def _mount(args: list[str]) -> str:
    return "" if set(args) <= {"-l", "-v", "-h", "--help", "-V", "--version"} else "changes the system"  # alone: lists


def _hostname(args: list[str]) -> str:
    sets = any(not a.startswith("-") for a in args) or set(args) & {"-F", "--file", "-b", "--boot"}
    return "changes the system" if sets else ""


def _service(args: list[str]) -> str:
    reads = args in (["--status-all"], ["--help"], ["-h"], ["--version"]) or len(args) == 2 and args[1] == "status"
    return "" if reads else "changes the system"


_CHECKS: dict[str, Callable[[list[str]], str]] = {
    "nmcli": _nmcli, "ip": _ip, "nft": _nft, "firewall-cmd": _firewall_cmd, "dpkg": _dpkg, "rpm": _rpm,
    "update-alternatives": _alternatives, "alternatives": _alternatives, "journalctl": _journalctl,
    "sysctl": _sysctl, "mount": _mount, "hostname": _hostname, "service": _service,
    **dict.fromkeys(("iptables", "ip6tables", "iptables-legacy", "ip6tables-legacy", "iptables-nft", "ip6tables-nft",
                     "ebtables", "arptables"), _iptables),
    **dict.fromkeys(("pacman", "yay", "paru"), _pacman),
}

# Where something written runs at login, or system-wide: persistence outside any sandbox's walls
_STARTUP_HOME = (".config/systemd", ".config/autostart", ".config/environment.d", ".local/share/systemd",
                 "Library/LaunchAgents")
_STARTUP_ROOTS = ("/etc", "/private/etc", "/Library/LaunchAgents", "/Library/LaunchDaemons")
_STARTUP_DOTFILES = {".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin",
                     ".xprofile", ".xinitrc", ".xsessionrc", ".pam_environment"}
_COPIERS = {"cp", "mv", "ln", "install", "rsync", "ditto"}  # write to their last operand (or -t DIR)
_WRITERS = {"tee", "touch", "mkdir", "patch"}  # write to every operand
_OUTPUT_FLAGS = {"curl": {"-o", "--output"}, "wget": {"-O", "--output-document", "-P", "--directory-prefix"},
                 "tar": {"-C", "--directory"}, "unzip": {"-d"}}
_WRITES_THERE = _COPIERS | _WRITERS | _OUTPUT_FLAGS.keys() | {"sed"}  # after a cd into such a folder
_REDIRECT_TARGETS = re.compile(r"(?<!<)>>?\|?\s*([^\s;&|()<>]+)")


def _starts_up(word: str) -> str:
    """Why writing to `word` changes what runs at login or system-wide, or ''."""
    word = word.strip("'\"")
    home = next((word[len(h):] for h in ("~/", "$HOME/", "${HOME}/", f"{Path.home()}/") if word.startswith(h)), None)
    if home is not None:
        rel = os.path.normpath(home)
        if rel in _STARTUP_DOTFILES:
            return "overwrites a settings file"
        if any(rel == d or rel.startswith(d + "/") for d in _STARTUP_HOME):
            return "changes the system"
    if word.startswith("/"):
        path = os.path.normpath(word)
        if any(path == d or path.startswith(d + "/") for d in _STARTUP_ROOTS):
            return "changes the system"
    return ""


def _writes_startup(program: str, args: list[str]) -> str:
    """Why a copy, tee, sed -i or download writes a login item, unit, autostart entry or /etc file, or ''."""
    operands = [a for a in args if not a.startswith("-")]
    if program in _COPIERS:
        targets = operands[-1:] + [b for a, b in zip(args, args[1:]) if a in ("-t", "--target-directory")]
        targets += [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
    elif program in _WRITERS or program == "sed" and any(
            a == "--in-place" or a.startswith("--in-place=") or re.fullmatch(r"-[A-Za-z]*i.*", a) for a in args):
        targets = operands
    elif program in _OUTPUT_FLAGS:
        targets = [b for a, b in zip(args, args[1:]) if a in _OUTPUT_FLAGS[program]]
        targets += [a.split("=", 1)[1] for a in args if a.split("=")[0] in _OUTPUT_FLAGS[program] and "=" in a]
    else:
        return ""
    return next((why for t in targets if (why := _starts_up(t))), "")


# `uv run` options taking a value; what follows them is the command it runs
_UV_RUN_VALUES = {"--with", "--with-editable", "--with-requirements", "-p", "--python", "--project", "--directory",
                  "--package", "--extra", "--group", "--only-group", "--no-group", "--index", "--default-index",
                  "--index-url", "--extra-index-url", "-f", "--find-links", "--env-file", "--exclude-newer",
                  "--config-file", "--cache-dir", "--color", "-P", "--upgrade-package", "--reinstall-package",
                  "--refresh-package", "--resolution", "--prerelease", "--link-mode", "--python-preference"}


def _strip_prefixes(argv: list[str]) -> list[str]:
    """argv minus wrappers (nohup, env, VAR=value, xargs -I {}, timeout 5, xcrun, uv run)."""
    while argv and (argv[0] in _PREFIXES or re.fullmatch(r"\w+=.*", argv[0])):
        argv = argv[1:]
        while argv and (argv[0].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", argv[0])):
            argv = argv[2:] if argv[0] in _PREFIX_VALUES else argv[1:]
    if argv and Path(argv[0]).name == "xcrun":  # the tool xcrun finds is the command
        return _strip_prefixes(_options_then(argv[1:], _XCRUN_VALUES))
    if argv and (Path(argv[0]).name, *argv[1:2]) in (("uv", "run"), ("poetry", "run"), ("pipenv", "run")):
        inner = _options_then(argv[2:], _UV_RUN_VALUES)
        if inner[:1] == ["--"]:
            inner = inner[1:]
        return _strip_prefixes(["python", *inner] if inner[:1] and inner[0].endswith(".py") else inner)
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
    if why := _writes_startup(program, args):
        return why
    if program in _READ_ONLY_SUBCOMMANDS:
        sub = _options_then(args, _SUB_VALUES.get(program, frozenset()))
        if sub and sub[0] in _READ_ONLY_SUBCOMMANDS[program] or not sub and program in _BARE_READS:
            return ""
        if program == "hostnamectl" and sub[0] in _HOSTNAME_PARTS and len(sub) == 1:
            return ""
        return _SUB_WHY.get(program, {}).get(sub[0] if sub else "", _READ_ONLY_WHY.get(program, "changes the system"))
    if program in _CHECKS:
        return _CHECKS[program](args)
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
        return "sends data off the computer"
    if program == "rsync":
        if any(a.startswith(("--delete", "--remove-source-files")) for a in args):
            return "deletes files"
        remote = any(re.match(r"[\w.@-]+:", a) for a in args if not a.startswith("-"))
        return "sends data off the computer" if remote else ""
    if program == "find":
        if "-delete" in flags:
            return "deletes files"
        runs = [i for i, a in enumerate(args) if a in ("-exec", "-execdir", "-ok", "-okdir")]
        return next((why for i in runs if (why := _argv_risk(args[i + 1 :]))), "")
    return ""


def _argv_risk(argv: list[str]) -> str:
    """Why a command another runs (find -exec) is risky: its program, or the code it runs, wherever it runs."""
    pieces = _parse(shlex.join(argv)) if argv else []
    if pieces is None:
        return "can't be checked"
    return next((why for p in pieces if (why := _piece(p.argv) or _code(p, None, _Reader()))), "")


# ── Reading a line: pieces, heredocs, pipes and folders ─────────


@dataclass
class _Piece:
    """One simple command, with what the line shows of where it runs and what it reads."""

    argv: list[str]
    raw: str = ""  # the shell code it came from: tells literal inline code from code the shell builds
    stdin: tuple[str, str, bool] | None = None  # (kind, value, literal): "text" (a heredoc, echo |), "file", "pipe"
    dirs: tuple[str, ...] | None = ()  # cd targets in effect, in order; None once one can't be read
    writes: list[str] = field(default_factory=list)  # redirect targets
    unseen: bool = False  # a shell here runs code the check can't read


@dataclass
class _Scan:
    text: str  # the line with its heredoc bodies cut out and backslash-newlines joined
    bodies: list[tuple[str, bool]]  # each heredoc the line's own commands read, and whether the shell expands it
    inner: list[str]  # the code of each $(...) and `...`, which runs wherever it appears, even in double quotes


_HEREDOC_OP = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|(\\?)([^\s;&|()<>'"`]+))""")
_OPERATORS = re.compile(r"\|\||&&|\|&|;;&?|[;&|()]")
_MAX_DEPTH = 8  # code within code (bash -c in a heredoc in a script...) this deep, and no deeper, is read
_SUBSTITUTED = "$_SUB_"  # stands in for a $(...) in the line around it: a word known only at run time


def _heredoc_body(command: str, i: int, delim: str, strip: bool) -> tuple[str, int]:
    """The heredoc body starting at `i`, up to its delimiter alone on a line, and where the line after it starts."""
    lines = []
    while i < len(command):
        end = command.find("\n", i)
        end = len(command) if end == -1 else end
        line, i = command[i:end], end + 1
        if (line.lstrip("\t") if strip else line) == delim:
            break
        lines.append(line.lstrip("\t") if strip else line)
    return "".join(line + "\n" for line in lines), min(i, len(command))


def _scan(command: str) -> _Scan | None:
    """Split a line's heredoc bodies and command substitutions from it, quote-aware: a << inside quotes is text, and
    inside $(...) quoting starts afresh. Each substitution leaves a placeholder word, so the line reads as the shell
    sees it after expanding them. None if a quote or substitution never closes."""
    out: list[str] = []
    bodies: list[tuple[str, bool]] = []
    inner: list[str] = []
    pending: list[tuple[bool, str, bool, bool]] = []  # (strip tabs, delimiter, quoted, the line's own)
    frames: list[list] = []  # [kind, start, out index or depth]: ' " $( ` ( ((
    i, n = 0, len(command)

    def substituted(frame: list) -> None:
        """Close a $(...) or `...`: its code is read on its own; the line keeps a word that isn't a literal."""
        if not any(f[0] in ("$(", "`") for f in frames):
            inner.append(command[frame[1] : i])
            del out[frame[2] :]
            out.append(_SUBSTITUTED)

    while i < n:
        c, kind = command[i], frames[-1][0] if frames else ""
        if c == "\n" and pending and kind not in ("'", '"'):
            out.append(c)
            i += 1
            for strip, delim, quoted, own in pending:
                start = i
                body, i = _heredoc_body(command, i, delim, strip)
                if own:
                    bodies.append((body, not quoted))
                else:
                    out.append(command[start:i])  # inside $(...): read with it
                if not quoted and own:  # the shell expands it: $(...) in it runs
                    found = _SUBSTITUTIONS.findall(body)
                    if body.count("$(") + body.count("`") // 2 > len(found):
                        return None
                    inner += ["".join(f) for f in found]
            pending = []
            continue
        if kind == "'":
            out.append(c)
            i += 1
            if c == "'":
                frames.pop()
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":  # a backslash-newline joins lines
                out.append(command[i : i + 2])
            i += 2
            continue
        if kind == "((" and not command.startswith("$(", i):  # arithmetic: << is a shift, not a heredoc
            frame = frames[-1]
            if command.startswith("))", i) and not frame[3]:
                frames.pop()  # a number at run time, or (( )) as a command: true or false
                del out[frame[2] :]
                out.append(_SUBSTITUTED if frame[4] else "true")
                i += 2
                continue
            if c in "()":
                frame[3] += 1 if c == "(" else -1
            out.append(c)
            i += 1
            continue
        step = 1
        if c == '"':
            if kind == '"':
                frames.pop()
            else:
                frames.append(['"', i, 0])
        elif command.startswith("$((", i) or command.startswith("((", i) and kind not in ('"', "(("):
            step = 3 if c == "$" else 2
            frames.append(["((", i, len(out), 0, c == "$"])
        elif command.startswith("$(", i):
            step = 2
            frames.append(["$(", i + 2, len(out)])
        elif c == "`":
            if kind == "`":
                substituted(frames.pop())
                i += 1
                continue
            frames.append(["`", i + 1, len(out)])
        elif kind == '"':
            pass  # only $( ) and ` ` are special inside double quotes
        elif c == "'":
            frames.append(["'", i, 0])
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = n if end == -1 else end  # a comment, to the end of the line: dropped, as the lines are joined
            continue
        elif c == "(":
            frames.append(["(", i, 0])
        elif c == ")" and kind in ("$(", "("):
            frame = frames.pop()
            if kind == "$(":
                substituted(frame)
                i += 1
                continue
        elif command.startswith("<<<", i):
            step = 3
        elif command.startswith("<<", i) and (m := _HEREDOC_OP.match(command, i)):
            quoted = m.group(2) is not None or m.group(3) is not None or bool(m.group(4))
            delim = next(g for g in (m.group(2), m.group(3), m.group(5)) if g is not None)
            pending.append((m.group(1) == "-", delim, quoted, all(f[0] == "(" for f in frames)))
            step = m.end() - i
        out.append(command[i : i + step])
        i += step
    if frames:
        return None
    bodies += [("", not quoted) for _, _, quoted, own in pending if own]  # never given a line: empty
    return _Scan("".join(out), bodies, inner)


def _literal(code: str, raw: str) -> bool:
    """Whether inline code is as written: nothing the shell expands, or single-quoted where it was written."""
    return not set(code) & set("$`") or f"'{code}'" in raw


def _cd(dirs: tuple[str, ...] | None, argv: list[str]) -> tuple[str, ...] | None:
    """The folders in effect after a cd/pushd/popd; None once they can't be read."""
    args = [a for a in argv[1:] if not re.fullmatch(r"-[LPqse@]+", a)]
    if dirs is None or argv[0] == "popd" or len(args) > 1 or args and (args[0] == "-" or _NOT_LITERAL & set(args[0])):
        return None
    return (*dirs, args[0] if args else "~")


def _piped(prev: _Piece) -> tuple[str, str, bool]:
    """What a piece piped from `prev` reads: echo's or printf's words, cat's heredoc or file, else another program."""
    argv = _strip_prefixes(prev.argv)
    name, args = (Path(argv[0]).name, argv[1:]) if argv else ("", [])
    if name in ("echo", "printf"):
        words = args[1:] if name == "echo" and args[:1] and re.fullmatch(r"-[neE]+", args[0]) else args
        text = " ".join(words) + "\n"
        return "text", text, "\\" not in text and all(_literal(w, prev.raw) for w in words)  # no escapes: no hiding
    if name == "cat" and not _operands(args) and prev.stdin:
        return prev.stdin
    if name == "cat" and len(_operands(args)) == 1:
        return "file", _operands(args)[0], True
    return "pipe", "", False


def _shell_code(piece: _Piece, depth: int) -> list[_Piece]:
    """The pieces of shell code a shell piece runs (bash -c, a heredoc), read as part of the line; marks the piece
    unseen when that code is built by the shell or can't be read."""
    argv = _strip_prefixes(piece.argv)
    if not argv or Path(argv[0].lstrip("\\")).name not in _SHELLS:
        return []
    run = _shell_run(argv[1:])
    if run.code is not None:
        code, literal = run.code, _literal(run.code, piece.raw)
    elif run.stdin and piece.stdin and piece.stdin[0] == "text":
        code, literal = piece.stdin[1], piece.stdin[2]
    else:
        return []  # a script file, or stdin from elsewhere: judged in _code
    inner = _parse(code, depth + 1, piece.dirs) if literal else None
    piece.unseen = inner is None
    return inner or []


def _parse(command: str, depth: int = 0, dirs: tuple[str, ...] | None = ()) -> list[_Piece] | None:
    """Every simple command in a line, with its stdin, folders and redirects: those in $(...) and `...`, and the shell
    code a shell in it runs. None on unbalanced quotes, or code nested too deep."""
    if depth > _MAX_DEPTH or (scan := _scan(command)) is None:
        return None
    found: list[_Piece] = []
    for inner in scan.inner:
        if (more := _parse(inner, depth + 1, dirs)) is None:
            return None
        found += more
    text = _FD_NUMBER.sub(" ", scan.text)  # the 2 in 2>/dev/null isn't an argument
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # _scan dropped them, line by line
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes
        return None
    bodies = iter(scan.bodies)
    used = 0
    cur = _Piece([], command, dirs=dirs)
    frames: list[tuple[str, _Piece | None, tuple[str, ...] | None]] = []  # ( ) subshells and <( ) substitutions
    redirect = ""

    def end() -> _Piece | None:
        nonlocal cur, dirs
        piece, cur = cur, _Piece([], command, dirs=dirs)
        if not piece.argv:
            return None
        found.append(piece)
        argv = _strip_prefixes(piece.argv)
        if argv and argv[0] in ("cd", "pushd", "popd", "chdir"):
            dirs = cur.dirs = _cd(dirs, argv)
        found.extend(_shell_code(piece, depth))
        return piece

    for token in tokens + [";"]:
        if redirect:
            op, redirect = redirect, ""
            if op == "<<<":
                cur.stdin = ("text", token + "\n", _literal(token, command))
            elif op.startswith("<<"):
                body, expanded = next(bodies, ("", False))
                used += 1
                cur.stdin = ("text", body, not expanded or not set(body) & set("$`"))
            elif op == "<":
                cur.stdin = ("file", token, True)
            elif ">" in op and not (op.endswith("&") and (token.isdigit() or token == "-")):
                cur.writes.append(token)
        elif token in ("<(", ">("):  # process substitution: the outer command reads a pipe
            cur.argv.append(f"{token})")
            frames.append(("<(", cur, dirs))
            cur = _Piece([], command, dirs=dirs)
        elif token and set(token) <= set(";&|()<>") and set(token) & set("<>"):
            redirect = token  # next word is a redirect target
        elif token and set(token) <= set(";&|()"):
            for op in _OPERATORS.findall(token):
                prev = end()
                if op == "(":
                    frames.append(("(", None, dirs))
                elif op == ")" and frames:
                    kind, outer, dirs = frames.pop()
                    cur.dirs = dirs
                    if kind == "<(" and outer is not None:
                        cur = outer
                elif op in ("|", "|&") and prev is not None:
                    cur.stdin = _piped(prev)
        else:
            cur.argv.append(token)
    if used != len(scan.bodies):
        return None  # a heredoc no command reads: the line isn't what it seems
    return found


@functools.lru_cache(maxsize=256)
def _line(command: str) -> tuple[_Piece, ...] | None:
    """_parse of a whole line, kept: one gate decision reads the same line several times. Never changed after."""
    pieces = _parse(command)
    return None if pieces is None else tuple(pieces)


def _simple_commands(command: str) -> list[list[str]] | None:
    """Every simple command as argv, including those in $(...) and `...` and shell code a shell in it runs; None on
    unbalanced quotes. Redirection targets and heredoc bodies are dropped, not treated as arguments."""
    pieces = _line(command)
    return None if pieces is None else [list(p.argv) for p in pieces]


# ── The code a piece runs ───────────────────────────────────────
# A script or inline code (python x.py, node -e, bash x.sh, ./x.sh, source x) is read and judged like the commands
# it holds (jarvis.safety.scripts). Unseen, it asks: downloaded and run on the spot, missing, too large, written
# earlier in the same line, built at run time, or in a language the check doesn't read.

_MAX_SCRIPT_BYTES = 256 * 1024  # one file
_MAX_READ_BYTES = 2 * 1024 * 1024  # all the files one check reads
_MAX_FILES = 64
_LIBRARIES = {"node_modules", "site-packages", "dist-packages", ".venv", "venv"}  # installed tools, trusted like programs
_BINARY_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa")
# Settings that make a program load code from elsewhere first
_CODE_ENV = re.compile(r"\b(NODE_OPTIONS|BASH_ENV|LD_PRELOAD|LD_LIBRARY_PATH|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH"
                       r"|PERL5OPT|RUBYOPT|PYTHONSTARTUP|PYTHONHOME)=")
_PY_VALUES = set("WX")  # python options taking a value
_NODE_LOADS = {"-r", "--require", "--import", "--loader", "--experimental-loader"}
_NODE_VALUES = {"-C", "--conditions", "--inspect-port", "--title", "--input-type", "--env-file", "--disable-warning",
                "--watch-path", "--redirect-warnings", "--test-reporter", "--test-reporter-destination", "--config",
                "-c", "--import-map", "--lock", "--cert", "--location", "--seed", "--v8-flags", "--unhandled-rejections"}
_BUN_COMMANDS = {"install", "i", "add", "remove", "rm", "update", "link", "unlink", "publish", "x", "test", "build",
                 "init", "create", "upgrade", "pm", "outdated", "patch", "repl", "exec", "info", "audit", "why"}
_GIT_WRITES = {"checkout", "switch", "restore", "reset", "pull", "merge", "rebase", "cherry-pick", "revert", "am",
               "apply", "stash", "clone", "mv", "rm", "worktree", "submodule"}


@dataclass
class _Run:
    """What code an interpreter command runs."""

    language: str  # shell, python, javascript; '' for one the check doesn't read
    code: str | None = None  # given inline (-c, -e)
    file: str | None = None  # a script, as written
    module: str | None = None  # python -m
    stdin: bool = False  # code read from stdin
    loads: tuple[str, ...] = ()  # files loaded first (node -r, bash --rcfile)


def _shell_run(args: list[str]) -> _Run:
    run, i, inline = _Run("shell"), 0, False
    while i < len(args):
        a = args[i]
        if a in ("-", "--"):
            i += 1
            break
        if a in ("--rcfile", "--init-file"):
            run.loads += tuple(args[i + 1 : i + 2])
            i += 2
        elif a.startswith("--command=") or a == "--command":  # fish
            inline = True
            if "=" in a:
                run.code = a.split("=", 1)[1]
                return run
            i += 1
        elif a[:1] in "-+" and len(a) > 1 and not a.startswith("--"):
            inline = inline or "c" in a
            run.stdin = run.stdin or "s" in a
            i += 1 + a.count("o") + a.count("O")  # -o pipefail: o takes the next word
        elif a.startswith("--"):
            i += 1
        else:
            break
    rest = args[i:]
    if inline:
        run.code = rest[0] if rest else ""
    elif rest and not run.stdin:
        run.file = rest[0]
    else:
        run.stdin = True
    return run


def _python_run(args: list[str]) -> _Run:
    run, i = _Run("python"), 0
    while i < len(args):
        a = args[i]
        if a == "-":
            run.stdin = True
            return run
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            i += 2 if a == "--check-hash-based-pycs" else 1
            continue
        if not a.startswith("-") or len(a) == 1:
            break
        i += 1
        for j, letter in enumerate(a[1:], 2):
            if letter in "cm" or letter in _PY_VALUES:
                value = a[j:] or (args[i] if i < len(args) else "")
                i += 0 if a[j:] else 1
                if letter == "c":
                    run.code = value
                    return run
                if letter == "m":
                    run.module = value
                    return run
                break
    if i < len(args):
        run.file = args[i]
    else:
        run.stdin = True
    return run


def _node_run(args: list[str], stdin: bool = True) -> _Run:
    """node's (and tsx's, bun's, deno run's) options, then the script. `stdin`: with no script, code comes from
    stdin (node), rather than nothing running (bun, deno)."""
    run, i = _Run("javascript"), 0
    while i < len(args):
        a = args[i]
        name, eq, value = a.partition("=")
        if a == "-":
            run.stdin = True
            return run
        if a == "--":
            i += 1
            break
        if not a.startswith("-"):
            break
        if name in ("-e", "--eval", "-p", "--print", "-pe"):
            run.code = value if eq else (args[i + 1] if i + 1 < len(args) else "")
            return run
        if name in _NODE_LOADS:
            run.loads += (value,) if eq else tuple(args[i + 1 : i + 2])
        i += 2 if not eq and (name in _NODE_LOADS or name in _NODE_VALUES) else 1
    if i < len(args):
        run.file = args[i]
    else:
        run.stdin = stdin
    return run


def _js_run(program: str, args: list[str]) -> _Run | None:
    if program in ("node", "nodejs", "tsx", "ts-node"):
        return _node_run(args)
    if program == "deno":
        if args[:1] == ["eval"]:
            code = _options_then(args[1:])
            return _Run("javascript", code=code[0] if code else "")
        if args[:1] == ["run"] or args and args[0].endswith(scripts.JS_EXTENSIONS):
            return _node_run(args[1:] if args[0] == "run" else args, stdin=False)
        return None  # deno task, fmt, test...: deno's own
    if args[:1] == ["run"]:  # bun run x.ts, or a package.json script
        args = args[1:]
    elif args[:1] and args[0] in _BUN_COMMANDS:
        return None
    run = _node_run(args, stdin=False)
    if run.file is not None and not run.file.endswith(scripts.JS_EXTENSIONS) and "/" not in run.file:
        run.file = None  # bun run dev: a package.json script, like npm run
    return run


def _interpreter(program: str, args: list[str]) -> _Run | None:
    """The code an interpreter runs, or None if `program` isn't one."""
    if program in _SHELLS:
        return _shell_run(args)
    if re.fullmatch(r"python[\d.]*|pypy[\d.]*", program):
        return _python_run(args)
    if program in _JS:
        return _js_run(program, args)
    if program in _OPAQUE:
        return _Run("")
    return None


def _shebang(head: bytes) -> str | None:
    """The language a #! line names, '' for one the check doesn't read; None if there's no #! line."""
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    if words and Path(words[0]).name == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    name = Path(words[0]).name if words else ""
    run = _interpreter(name, [])
    return run.language if run is not None else ""


def _where(piece: _Piece, cwd: Path | None) -> Path | None:
    """The folder a piece runs in, from `cwd` and the line's cds; None if unknown."""
    if piece.dirs is None:
        return None
    where = cwd
    for d in piece.dirs:
        path = Path(d).expanduser()
        where = path if path.is_absolute() else where / path if where is not None else None
    return where


def _script_path(word: str, where: Path | None) -> Path | None:
    """The file a word names, or None if the shell would expand it or the folder is unknown."""
    if (path := _Mine._literal(word)) is None:
        return None
    return path if path.is_absolute() else where / path if where is not None else None


@dataclass(frozen=True)
class Seen:
    """The script files one check read: each one's fingerprint, to tell later whether it changed, and whether any of
    them touches secrets."""

    files: dict[str, str]  # resolved path -> sha256 of its bytes, or why it wasn't read ('missing', 'too large'...)
    secret: bool


class _Reader:
    """Reads and judges the scripts one check meets, within a budget, keeping each file's fingerprint."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.judged: dict[tuple[str, str], str] = {}
        self.bytes = 0
        self.depth = 0
        self.secret = False
        self.written: list[tuple[Path, str | None]] = []  # paths lines write (with the text, from a heredoc)

    def seen(self) -> Seen:
        return Seen(dict(self.files), self.secret)

    def _bytes(self, path: Path, head_only: bool = False) -> bytes | None:
        """A regular file's bytes (or first 4 KB), or None, noting why in `files`. Never a secret, a pipe or a device."""
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            self.files[str(path)] = "missing"
            return None
        key = str(real)
        if secret_path(real):
            self.secret, self.files[key] = True, "secret"
            return None
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self.files[key] = "unreadable"
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self.files[key] = "not a file"
                return None
            if head_only:
                return os.read(fd, 4096)
            if (info.st_size > _MAX_SCRIPT_BYTES or self.bytes + info.st_size > _MAX_READ_BYTES
                    or len(self.files) >= _MAX_FILES):
                self.files[key] = "too large"
                return None
            data = b""
            while len(data) <= _MAX_SCRIPT_BYTES and (chunk := os.read(fd, 65536)):
                data += chunk
        except OSError:
            self.files[key] = "unreadable"
            return None
        finally:
            os.close(fd)
        if len(data) > _MAX_SCRIPT_BYTES:
            self.files[key] = "too large"
            return None
        self.bytes += len(data)
        self.files[key] = hashlib.sha256(data).hexdigest()
        return data

    def file(self, path: Path, language: str, roots: tuple[Path, ...] | None = None) -> str:
        """Why a local script file needs a yes; UNSEEN if it can't be read or was written earlier in the line."""
        key = (str(path), language)
        if key in self.judged:
            return self.judged[key]
        if written := [text for w, text in self.written if path == w or path.is_relative_to(w)]:
            # cat > x.py <<EOF ... python x.py: the heredoc is the script
            return self.code(written[0], language, path, roots) if len(written) == 1 and written[0] is not None \
                else UNSEEN
        if _LIBRARIES & set(path.parts):
            return ""
        self.judged[key] = ""  # a cycle back here adds nothing
        data = self._bytes(path)
        try:
            text = None if data is None else data.decode()
        except UnicodeDecodeError:
            text = None
        why = (scripts.SECRETS if self.files.get(str(path.resolve())) == "secret" else UNSEEN) if text is None \
            else self.code(text, language, path, roots)
        self.judged[key] = why
        return why

    def executable(self, path: Path) -> str:
        """Why a file run by its path (./x.sh) needs a yes: its #! line says how to read it; a program is trusted."""
        if _LIBRARIES & set(path.parts):
            return ""
        if any(path == w or path.is_relative_to(w) for w, _ in self.written):
            return UNSEEN
        if (head := self._bytes(path, head_only=True)) is None:
            return scripts.SECRETS if self.files.get(str(path.resolve())) == "secret" else UNSEEN
        if head.startswith(_BINARY_MAGIC) or b"\0" in head:
            return ""  # a built program, like any other
        language = _shebang(head)
        return self.file(path, "shell" if language is None else language) if language != "" else UNSEEN

    def code(self, text: str, language: str, path: Path | None, roots: tuple[Path, ...] | None,
             where: Path | None = None) -> str:
        """Why code in `language` needs a yes. `where`: the folder it runs in (a file's spawned commands resolve
        there too: the caller's, or its own folder if unknown)."""
        if self.depth >= _MAX_DEPTH:
            return UNSEEN
        where = where if where is not None else path.parent if path else None
        self.depth += 1
        try:
            ctx = _Context(self, where)
            if language == "shell":
                why = scripts.SECRETS if ctx.secret(text) else ctx.shell(text)
            elif language == "python":
                why = scripts.python(text, path, roots if roots is not None else (path.parent,) if path else None, ctx)
            elif language == "javascript":
                why = scripts.javascript(text, path, where, ctx)
            else:
                why = UNSEEN
        finally:
            self.depth -= 1
        self.secret = self.secret or why == scripts.SECRETS
        return why


class _Context:
    """scripts.Context for one script: its spawned commands go back through the check, in the folder it runs in."""

    def __init__(self, reader: _Reader, where: Path | None) -> None:
        self.reader, self.where = reader, where

    def shell(self, line: str) -> str:
        found = _check(line, None, self.where, self.reader)
        why = next((why for _, why in found), "")
        return UNSEEN if why == "can't be checked" else why

    def secret(self, text: str) -> bool:
        return (names_secret(text) or reads_environment(text) or bool(_CONTROLS.search(text))
                or bool(_SECRET_SERVICE.search(text)))

    def startup(self, text: str) -> str:
        return _starts_up(text) or _names_startup(text)

    def follow(self, path: Path, language: str, roots: tuple[Path, ...] | None) -> str:
        return self.reader.file(path, language, roots)


_STARTUP_NAMES = re.compile(r"(^|/)(" + "|".join(re.escape(d) for d in _STARTUP_DOTFILES) + r")$")
_STARTUP_PARTS = re.compile("|".join(re.escape(d) for d in _STARTUP_HOME) + r"|^(/private)?/etc(/|$)|^/Library/Launch")


def _names_startup(text: str) -> str:
    """Why a path a script builds (Path.home() / '.zshrc') is a login item or settings file, or ''."""
    if _STARTUP_NAMES.search(text):
        return "overwrites a settings file"
    return "changes the system" if _STARTUP_PARTS.search(text) else ""


def _script_why(why: str) -> str:
    """A script's risk, as said of the command that runs it."""
    if why in ("", UNSEEN) or why.startswith("runs a script that "):
        return why
    return f"runs a script that {why}"


def _code(piece: _Piece, cwd: Path | None, reader: _Reader) -> str:
    """Why the code a piece runs needs a yes: its script or inline code, read and judged; UNSEEN if it can't be."""
    argv = _strip_prefixes(piece.argv)
    if not argv:
        return ""
    word = argv[0].lstrip("\\")
    if set(word) & set("$`") or piece.unseen:
        return UNSEEN  # a program named at run time, or shell code built at run time
    program, args = Path(word).name, argv[1:]
    if args and set(args) <= _INFO:
        return ""
    where = _where(piece, cwd)
    if program in ("source", "."):
        return _script_why(reader.file(path, "shell") if (path := _script_path(args[0], where)) else UNSEEN) \
            if args else ""
    if (run := _interpreter(program, args)) is None:
        if "/" not in word:
            return ""  # a program on the PATH
        if (path := _script_path(word, where)) is None:
            return UNSEEN
        if Path(word).expanduser().is_absolute() and not any(
                path.is_relative_to(d) for d in (*_TEMP_DIRS, *([where] if where else []))):
            return ""  # a program installed elsewhere on the computer
        return _script_why(reader.executable(path))
    if not run.language:
        return UNSEEN
    lead = piece.argv[: len(piece.argv) - len(argv)]
    if any(_CODE_ENV.match(w) for w in lead):
        return UNSEEN
    roots = _python_roots(lead, where)
    whys = [reader.file(p, run.language) if (p := _script_path(f, where)) else UNSEEN for f in run.loads
            if run.language == "shell" or f.startswith((".", "/", "~"))]  # node -r a-package: a library
    if run.code is not None:
        if run.language != "shell":  # shell code was read as part of the line
            whys.append(reader.code(run.code, run.language, None, roots, where) if _literal(run.code, piece.raw)
                        else UNSEEN)
    elif run.module is not None:
        whys.append(_module(run.module, roots, reader))
    elif run.file is not None:
        path = _script_path(run.file, where)
        file_roots = None if roots is None or path is None else (path.parent, *roots[1:])
        whys.append(reader.file(path, run.language, file_roots) if path else UNSEEN)
    elif run.stdin:
        kind, value, literal = piece.stdin or ("pipe", "", False)
        if kind == "file":
            whys.append(reader.file(p, run.language, roots) if (p := _script_path(value, where)) else UNSEEN)
        elif kind == "text" and literal:
            if run.language != "shell":
                whys.append(reader.code(value, run.language, None, roots, where))
        else:
            whys.append(UNSEEN)  # a download, a terminal, another program's output
    return _script_why(scripts.first(whys))


# Where a script run by its full path is the line's own, not an installed program
_TEMP_DIRS = {p for d in (tempfile.gettempdir(), "/tmp") for p in (Path(d), Path(d).resolve())}


def _python_roots(lead: list[str], where: Path | None) -> tuple[Path, ...] | None:
    """Where Python finds local modules: the working folder (a script's own folder replaces it), then PYTHONPATH.
    None if unknown."""
    roots = [where] if where is not None else None
    for word in lead:
        if word.startswith("PYTHONPATH="):
            for part in word.split("=", 1)[1].split(":"):
                if (path := _script_path(part or ".", where)) is None or roots is None:
                    return None
                roots.append(path)
    return tuple(roots) if roots is not None else None


def _module(module: str, roots: tuple[Path, ...] | None, reader: _Reader) -> str:
    """Why python -m MODULE needs a yes: a local module or package is read; an installed one is a library."""
    if roots is None:
        return "" if module.split(".")[0] in sys.stdlib_module_names else UNSEEN
    parts = module.split(".")
    for root in roots:
        base = root.joinpath(*parts)
        for path in (base.with_suffix(".py"), base / "__main__.py"):
            if path.is_file():
                return reader.file(path, "python", roots)
    return ""


def _line_writes(pieces: tuple[_Piece, ...], cwd: Path | None) -> list[tuple[Path, str | None]]:
    """Files and folders a line writes (redirects, copies, downloads, git checkouts), each with its text when a
    heredoc gives it (cat > x.py <<EOF): a script among them can't be read before it runs."""
    found = []
    for piece in pieces:
        where = _where(piece, cwd)
        argv = _strip_prefixes(piece.argv)
        program, args = (Path(argv[0]).name, argv[1:]) if argv else ("", [])
        text = piece.stdin[1] if piece.stdin and piece.stdin[0] == "text" and piece.stdin[2] else None
        targets = [(t, text if program == "cat" and not _operands(args) else None) for t in piece.writes]
        operands = _operands(args)
        if program in _COPIERS:
            targets += [(t, None) for t in operands[-1:] + [b for a, b in zip(args, args[1:]) if a in ("-t", "--target-directory")]]
        elif program in _WRITERS or program in ("sed", "dd", "perl"):
            targets += [(t.removeprefix("of="), text if program == "tee" else None) for t in operands]
        elif program in _OUTPUT_FLAGS:
            given = [b for a, b in zip(args, args[1:]) if a in _OUTPUT_FLAGS[program]]
            targets += [(t, None) for t in given or (["."] if program != "curl" or {"-O", "--remote-name"} & set(args)
                                                     or "-OJ" in args else [])]
        elif program == "git" and (sub := _options_then(args, {"-C", "-c"})) and sub[0] in _GIT_WRITES:
            targets.append((".", None))
        for target, given in targets:
            if (path := _script_path(target, where)) is not None:
                found.append((path, given))
    return found


def risky_pieces(command: str, own: "Own | None" = None, cwd: Path | None = None) -> list[tuple[list[str], str]]:
    """[(unwrapped argv, why)] for each risky simple command, in order; [] if safe.

    With `own`, a task's own deletes/kills are exempt. Whole-line risks (dotfile clobber, unparsable) come as ([], why).
    `cwd` (default: own's folder) is where the line runs, for reading the scripts it runs; unknown, relative ones are
    unseen.
    """
    return _check(command, own, cwd if cwd is not None or own is None else own.folder, _Reader())


def seen(command: str, cwd: Path | None = None) -> Seen:
    """The script files the check reads for this line, fingerprinted, and whether any touches secrets."""
    reader = _Reader()
    _check(command, None, cwd, reader)
    return reader.seen()


def _check(command: str, own: "Own | None", cwd: Path | None, reader: _Reader) -> list[tuple[list[str], str]]:
    if _CLOBBER_DOTFILE.search(command):
        return [([], "overwrites a settings file")]
    if (pieces := _line(command)) is None:
        return [([], "can't be checked")]
    if why := next((why for t in _REDIRECT_TARGETS.findall(command) if (why := _starts_up(t))), ""):
        return [([], why)]  # > /etc/hosts
    argvs = [p.argv for p in pieces]
    # cd ~/.config/systemd/user && cp x.service .: every write there counts
    there = any(p[0] in ("cd", "pushd") and len(p) > 1 and _starts_up(p[1]) for p in map(_strip_prefixes, argvs) if p)
    if there and _REDIRECT_TARGETS.search(command):
        return [([], "changes the system")]
    reader.written += _line_writes(pieces, cwd)
    found, mine = [], None
    for piece in pieces:
        argv = piece.argv
        why = _piece(argv) or _code(piece, cwd, reader)
        if not why and there and Path((_strip_prefixes(argv) or argv)[0]).name in _WRITES_THERE:
            why = "changes the system"
        if not why:
            continue
        if own and (mine := mine or _Mine(command, argvs, own)).covers(argv):
            continue
        found.append((list(_strip_prefixes(argv) or argv), why))
    return found


def _risk(command: str, own: "Own | None" = None, cwd: Path | None = None) -> tuple[str, str]:
    """(why, program) for the first risky piece; ('', '') if safe."""
    if not (found := risky_pieces(command, own, cwd)):
        return "", ""
    argv, why = found[0]
    if not argv:
        return why, "unbalanced quotes" if why == "can't be checked" else "a redirect"
    return why, program(argv)


def program(argv: list[str]) -> str:
    """Program plus subcommand, if any: 'brew install', 'git push', 'rm'."""
    argv = _strip_prefixes(argv) or argv
    words = [Path(argv[0].lstrip("\\")).name]
    rest = _options_then(argv[1:], {"-C", "-c", "-R", "--repo", *_SUB_VALUES.get(words[0], ())})
    if rest and re.fullmatch(r"[a-z][a-z-]{1,20}", rest[0]) and words[0] not in _ALWAYS.keys() | _OPTION_DRIVEN:
        words.append(rest[0])
    return " ".join(words)


PROGRAM_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}")  # a program found on the PATH, safe in a shell line


def programs(command: str) -> list[str]:
    """The programs a command runs by name (found on the PATH, not ./script), wrappers like env and nohup skipped, in
    order, each once; [] if it can't be read."""
    found = (argv[0] for argv in map(_strip_prefixes, _simple_commands(command) or []) if argv)
    return list(dict.fromkeys(name for name in found if PROGRAM_NAME.fullmatch(name)))


# Programs that wait on the network or a build, often for minutes: run as jobs, never in a spoken turn. (Installs
# keep their in-turn yes; the shell prompt sends long ones to start_job.)
_SLOW = dict.fromkeys(["git pull", "git fetch", "git clone", "git submodule", "docker pull"],
                     "fetches over the network") | {"docker build": "builds an image"}


def slow(command: str) -> str:
    """Why a command may well take minutes (network git, docker pulls and builds), or ''."""
    for argv in _simple_commands(command) or []:
        if argv and (why := _SLOW.get(program(argv))):
            return why
    return ""


def risky(command: str, own: "Own | None" = None, cwd: Path | None = None) -> str:
    """Why a command needs a yes ('deletes files', ...), or ''. Errs toward asking: unreadable means risky.

    `own`: a background task's deletes in its folders and kills of its processes need no yes. `cwd`: where it runs
    (default: own's folder), for reading the scripts it runs.
    """
    return _risk(command, own, cwd)[0]


def same_risk(a: str, b: str) -> bool:
    """Whether two lines do the same risky things in the same folders, differing only in wrappers (| tail, 2>&1).

    False if either is safe or unparsable.
    """
    def risk(command: str) -> tuple | None:
        found = risky_pieces(command)
        if not found or any(not argv for argv, _ in found) or (pieces := _simple_commands(command)) is None:
            return None
        folders = [p for p in map(_strip_prefixes, pieces) if p and p[0] in ("cd", "pushd", "popd", "chdir")]
        return found, folders

    return (mine := risk(a)) is not None and mine == risk(b)


def describe(command: str, own: "Own | None" = None, cwd: Path | None = None) -> str:
    """'installs software (brew install)', from the command itself (never a model's summary); '' if safe."""
    why, what = _risk(command, own, cwd)
    return f"{why} ({what})" if why else ""


# Package managers that sandbox their own build steps; macOS sandboxes can't nest, so an approved install runs
# outside the host profile.
_OWN_SANDBOX = {"brew", "port"}
_STDIN_FILTERS = {"head", "tail"}  # allowed to trim output, if reading no file


def own_sandbox(command: str) -> bool:
    """Whether a line is only a brew/port install (plus cd, or head/tail on its output).

    Approved, such a line runs outside the host profile, so nothing may ride along: bare program names, no prefixes,
    no substitution, no redirect but 2>&1.
    """
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
# Words that plainly order what a risky command does ("push it", "delete the build folder") are its go-ahead, as is
# a clear yes to JARVIS's question proposing it. Only _VERBS count, and each must name its targets: every path for a
# delete, every package for an install. A question, no, hedge or condition voids the order. Other risks (network,
# system settings, opaque code, discarding git work, secrets) have no order words and are always asked.

_ORDER_MOST_WORDS = 40
_ORDER_NOT = set("""no nope nah not don't dont never wait hold hang cancel abort unless if but except later maybe
perhaps should shall whether might before after until once when first instead without rather tomorrow can't cannot
won't shouldn't isn't wouldn't didn't haven't""".split())
# A choice or condition in JARVIS's question: a yes doesn't say which
_QUESTION_NOT = set("""or no not don't dont never without instead unless if before after until once first""".split())
_VERBS = {  # user's word -> what it orders
    "push": {"push"}, "restart": {"restart"}, "reload": {"restart"}, "relaunch": {"restart"}, "kickstart": {"restart"},
    "stop": {"stop"}, "kill": {"stop"}, "quit": {"stop"}, "close": {"stop"}, "terminate": {"stop"}, "shut": {"stop"},
    "start": {"start"}, "launch": {"start"}, "install": {"install"}, "reinstall": {"install"},
    "uninstall": {"uninstall"}, "remove": {"delete", "uninstall"}, "upgrade": {"upgrade"}, "update": {"upgrade"},
    "sync": {"sync"}, "delete": {"delete"}, "trash": {"delete"}, "erase": {"delete"}, "wipe": {"delete"},
    "merge": {"merge"}, "create": {"open"}, "open": {"open"}, "raise": {"open"}, "commit": {"commit"},
}
# "commit" orders one only as a verb on the work, or ending the clause: "commit it", "commit and push", "make a commit".
# Not a commit named ("the last commit", "from commit abc123", "commit messages", "pre-commit") or the user's own
# ("I'll commit it")
_NOT_BEFORE_COMMIT = set("""the that this last latest previous recent my your its their his her which what whose each
every first next one from since revert pre git i i'll i'd we we'll we'd they he she""".split())
_AFTER_COMMIT = set("it this that them these those the changes change everything all your my our please now too and "
                    "then".split()) | {""}
_SERVICES = {  # order -> allowed launchctl / systemctl / brew services / docker / podman subcommands
    "restart": {"kickstart", "kill", "bootout", "bootstrap", "load", "unload", "stop", "start", "restart", "reload",
                "try-restart", "reload-or-restart", "try-reload-or-restart"},
    "stop": {"bootout", "unload", "stop", "kill"},
    "start": {"bootstrap", "load", "start", "kickstart", "run"},
}
_PACKAGES = {  # order -> allowed package-manager subcommands
    "install": {"install", "i", "ci", "add", "reinstall", "sync"},
    "uninstall": {"uninstall", "remove", "rm", "un"},
    "upgrade": {"upgrade", "update", "up"},
    "sync": {"sync"},
}
_PULL_REQUEST = {"pr", "prs", "pull", "mr", "request"}
_PUSH_DELETES = {"-d", "--delete", "--prune", "--mirror"}  # delete on the remote: never ordered
_DELETERS = {"rm", "rmdir", "unlink", "trash"}
_CONTAINER_REMOVES = {"rm", "rmi", "prune", "down", "pause"}  # beyond a restart or stop


def _acts(words: list[str]) -> set[str]:
    """Acts ordered by these words' verbs."""
    return {act for i, w in enumerate(words) for act in _VERBS.get(w, ()) if w != "commit" or _commits(words, i)}


def _commits(words: list[str], i: int) -> bool:
    """Whether words[i], "commit", orders one (see _AFTER_COMMIT)."""
    before, after = words[i - 1] if i else "", words[i + 1] if i + 1 < len(words) else ""
    return before not in _NOT_BEFORE_COMMIT and after in _AFTER_COMMIT


def _order_words(said: str) -> list[str] | None:
    """The words, or None if they can't be an order (question, no, hedge, condition)."""
    if "?" in said:
        return None
    words = _words(said)
    if not words or len(words) > _ORDER_MOST_WORDS or _ORDER_NOT.intersection(words):
        return None
    return words


def orders(said: str) -> set[str]:
    """Acts the words order ('push', 'restart', ...); empty if not an order."""
    return _acts(_order_words(said) or [])


# Words that hold a commit back ("don't commit yet", "I'll commit it myself"), unlike one merely named ("the last
# commit", "did it commit?")
_HOLDS_COMMIT = set("no not don't dont never without hold wait stop skip myself i'll".split())


def holds_commit(said: str) -> bool:
    """Whether the words hold a commit back without ordering one ("don't commit yet"): a commit ordered before lapses.
    A commit only named or asked about ("note the commit hash", "did it commit?") leaves an order as it was."""
    words = _words(said)
    return "commit" in words and "commit" not in orders(said) and not _HOLDS_COMMIT.isdisjoint(words)


def _named(word: str, said: set[str]) -> bool:
    """Whether `said` contains one of `word`'s parts of 3+ letters."""
    return any(len(w) >= 3 and w in said for w in re.findall(r"[a-z0-9]+", word.lower()))


def _path_named(word: str, said: set[str]) -> bool:
    """Whether `said` names path `word` by its own name; never home, a top-level folder, a glob or a $VAR."""
    if _NOT_LITERAL & set(word):
        return False
    path = Path(word).expanduser()
    if path.name in ("", ".", "..") or path == Path.home() or (path.is_absolute() and len(path.parts) <= 3):
        return False
    return _named(path.name, said)


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _push_as_said(rest: list[str], said: set[str]) -> bool:
    """git push args: never a remote delete; forced only if "force" was said."""
    if _PUSH_DELETES & set(rest) or any(a.startswith((":", "+:")) for a in rest):
        return False
    forced = any(a in ("-f", "--force") or a.startswith(("--force", "+")) for a in rest)
    return not forced or "force" in said


def _install_as_said(words: list[str], acts: set[str], said: set[str]) -> bool:
    """Package-manager words (from the subcommand): the ordered act, on named packages only."""
    i = next((i for i, w in enumerate(words) if any(w in subs for subs in _PACKAGES.values())), None)
    if i is None:
        return False
    return (any(words[i] in _PACKAGES[act] for act in acts & _PACKAGES.keys())
            and all(_named(p, said) for p in _operands(words[i + 1:])))


def _ordered_piece(argv: list[str], why: str, acts: set[str], said: set[str]) -> bool:
    """Whether one risky piece does exactly what was ordered."""
    name, args = Path(argv[0].lstrip("\\")).name, argv[1:]
    words = _options_then(args, {"-C", "-c", "--git-dir", "--work-tree", "-m", "-R", "--repo"})  # from subcommand
    sub, rest = (words or [""])[0], words[1:]
    if name == "git" and sub == "push":
        return "push" in acts and _push_as_said(rest, said)
    if why == "deletes files":
        paths = _operands(args if name in _DELETERS else rest if (name, sub) == ("git", "rm") else [])
        return "delete" in acts and bool(paths) and all(_path_named(p, said) for p in paths)
    if why == "stops a process" and name in ("kill", "pkill", "killall"):
        targets = _operands(args)  # kill takes pids, which can't be named
        return "stop" in acts and (name == "kill" or bool(targets) and all(_named(t, said) for t in targets))
    if why in ("stops a process", "changes the system") and (name in ("launchctl", "systemctl", "docker", "podman")
                                                            or (name, sub) == ("brew", "services")):
        if name in ("docker", "podman") and set(args) & _CONTAINER_REMOVES:
            return False
        units = [re.sub(r"\.(service|socket|timer|target|path|mount)$", "", u) for u in _operands(rest)]
        if name == "systemctl" and not (units and all(_named(u, said) for u in units)):
            return False  # each unit by name: "restart nginx"
        does = {"brew": rest[:1], "launchctl": [sub], "systemctl": [sub]}.get(name, args)
        return any(set(does) & _SERVICES[act] for act in acts & _SERVICES.keys())
    if why == "installs software":
        return _install_as_said(words, acts, said)
    if why.startswith("changes things on ") and sub in ("pr", "mr") and rest:
        return {"merge": "merge", "create": "open"}.get(rest[0]) in acts and bool(_PULL_REQUEST & said)
    return False


def _does_as_said(command: str, acts: set[str], said: set[str], own: "Own | None") -> bool:
    """Whether every risky piece does just what `acts` order, on things named in `said`."""
    if not acts or names_secret(command) or reads_environment(command):
        return False
    found = risky_pieces(command, own)
    return bool(found) and all(argv and _ordered_piece(argv, why, acts, said) for argv, why in found)


def ordered(said: str, command: str, own: "Own | None" = None) -> bool:
    """Whether the words plainly order every risky piece ('push it' for git push, 'install ffmpeg' for brew).

    Never a question, no or condition, nor a risk kind with no order words.
    """
    return _does_as_said(command, orders(said), set(_words(said)), own)


def agreed_to(asked: str, said: str, command: str, own: "Own | None" = None, yes: bool | None = None) -> bool:
    """Whether `said` is a clear yes to the last sentence of `asked`, a question proposing exactly `command`.

    Questions offering a choice or condition don't count. `yes` overrides the word lists when known (consent).
    """
    question = re.split(r"(?<=[.!?])\s+", asked.strip())[-1]
    if not question.endswith("?") or not (yes_scope(said) is not None if yes is None else yes):
        return False
    words = _words(question)
    if _QUESTION_NOT.intersection(words):
        return False
    return _does_as_said(command, _acts(words), set(words) | set(_words(said)), own)


# ── A task's own files and processes ────────────────────────────
# No yes needed only where the command itself proves ownership: each path is literal (or a fresh mktemp dir) and
# resolves, symlinks included, strictly inside a task folder and outside .git; each process is $! or Own.process.

@dataclass(frozen=True)
class Own:
    """A task's own: start folder, folders it may delete in (worktree/project, temp), and its-process predicate."""

    folder: Path
    roots: tuple[Path, ...]
    process: Callable[[int], bool] = lambda pid: False


# Any of these can change what a word or path means (aliases, glob options, CDPATH, links, moves): nothing is owned
_UNSURE = re.compile(r"\b(alias|unalias|function|setopt|unsetopt|emulate|hash|enable|disable|CDPATH|IFS|GLOBIGNORE)\b"
                     r"|\(\s*\)|\bset\s+[-+]")
_UNSURE_PROGRAMS = {"ln", "mv", "xargs", "parallel"}  # xargs/parallel add their own arguments
_NOT_LITERAL = set("$`{}[]\\*?")
_MKTEMP = r"\"?\$\(\s*mktemp((\s+-[dq]+)*(\s+-[dq]*t\s+[\w.-]+)?(\s+-[dq]+)*)\s*\)\"?"  # fresh dir in $TMPDIR
_JOB = re.compile(r"%(\d+|%|\+|-)")
_DELETE_FLAGS = {"rm": set("dfiIPRrvWx"), "rmdir": {"v"}, "unlink": set()}  # allowed options


class _Mine:
    """Ownership check for one command line against a task's `Own`."""

    def __init__(self, command: str, pieces: list[list[str]], own: Own) -> None:
        self.own = own
        self.roots = [r.resolve() for r in own.roots]
        self.guarded = [*self.roots, ROOT.resolve()]  # never deleted whole, nor anything containing them
        words = {Path(w).name for argv in pieces for w in argv}  # incl. programs run by others (find -exec mv)
        self.sure = not _UNSURE.search(command) and not words & _UNSURE_PROGRAMS
        self.pids = self._reads(self._set_only(command, r"\$!"))  # vars only ever set to $!
        self.temps = self._reads(self._set_only(command, _MKTEMP))  # vars only ever set to a fresh mktemp dir
        self.cwds = self._cwds(pieces)

    @staticmethod
    def _set_only(command: str, value: str) -> set[str]:
        """Variables only ever set to `value` and otherwise only read as $NAME."""
        names = set()
        for name in {m.group(1) for m in re.finditer(rf"(?<![\w$])([A-Za-z_]\w*)=(?:{value})(?=[\s;&|)]|$)", command)}:
            sets = len(re.findall(rf"(?<![\w$]){name}=(?:{value})(?=[\s;&|)]|$)", command))
            reads = len(re.findall(rf"\$(?:{name}\b(?![\[(:])|\{{{name}\}})", command))
            if sets + reads == len(re.findall(rf"\b{name}\b", command)):
                names.add(name)
        return names

    @staticmethod
    def _reads(names: set[str]) -> set[str]:
        """$NAME and ${NAME} forms of these variables."""
        return {f"${n}" for n in names} | {f"${{{n}}}" for n in names}

    @staticmethod
    def _literal(word: str) -> Path | None:
        """The path a word names if the shell won't expand it (except a leading ~); else None."""
        if word == "~" or word.startswith("~/"):
            word = str(Path.home()) + word[1:]
        if not word or _NOT_LITERAL & set(word) or word[0] in "=~":
            return None
        return Path(word)

    def _cwds(self, pieces: list[list[str]]) -> set[Path] | None:
        """Every folder a piece might run in; None if unknowable."""
        start = self.own.folder.resolve()
        cds = [a for a in (_strip_prefixes(p) for p in pieces) if a and a[0] in ("cd", "pushd", "popd", "chdir")]
        found = {start}
        for argv in cds:
            if argv[0] == "popd":
                continue  # already counted
            args = [a for a in argv[1:] if not re.fullmatch(r"-[LPqs]+", a)]
            if not args:
                found.add(Path.home().resolve())
                continue
            if len(args) > 1 or args[0][0] in "-+" or (target := self._literal(args[0])) is None:
                return None
            if not target.is_absolute():
                if len(cds) > 1:
                    return None  # base depends on run order
                target = start / target
            found.add(target.resolve())
        return found

    def _inside(self, path: Path, strictly: bool = True) -> bool:
        """Whether a resolved path is inside a task folder (strictly: not the root), outside .git, and contains no
        guarded folder."""
        for root in self.roots:
            if path.is_relative_to(root) and ".git" not in path.relative_to(root).parts:
                return not any(g.is_relative_to(path) and (strictly or g != path) for g in self.guarded)
        return False

    def _where(self, path: Path) -> list[Path] | None:
        """The path resolved from each possible cwd; None if cwds are unknown."""
        if path.is_absolute():
            return [path.resolve()]
        return None if self.cwds is None else [(cwd / path).resolve() for cwd in self.cwds]

    def _owned_path(self, word: str) -> bool:
        if word in self.temps:
            return True
        head, tail = os.path.split(word.rstrip("/") if word.strip("/") else word)
        if set("*?") & set(tail) and not _NOT_LITERAL & set(tail.replace("*", "").replace("?", "")):
            # glob in the last part only; never one that could match .git or a dotfile
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
        """Whether a risky simple command only deletes the task's own files or stops its own processes."""
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

# JARVIS's controls: the runner socket and the approvals endpoint
_CONTROLS = re.compile(r"runner\.sock|/api/approvals|/approvals/\d")
_KEYCHAIN_DUMPS = re.compile(r"find-(generic|internet)-password|dump-keychain|export|unlock-keychain|set-key-partition-list"
                             r"|add-(generic|internet)-password|delete-(generic|internet)-password")
# Linux keychains: the Secret Service (GNOME Keyring, KeePassXC), KWallet, pass, and either over D-Bus
_SECRET_TOOL_USES = {"lookup", "search", "store", "clear"}
_SECRET_SERVICE = re.compile(r"org\.freedesktop\.secrets|org\.freedesktop\.Secret\b|org\.kde\.kwalletd|org\.gnome\.keyring")
_ROOT = {"sudo", "su", "doas", "pkexec", "run0"}
# Linux: the task sandbox shuts out the user's D-Bus, so these would only fail with "Failed to connect to bus"
_SERVICE_TARGETS = re.compile(r"services-[\w-]+")
USER_BUS = ("it needs the user's own session (systemd --user), which tasks can't reach: ask the user to run it "
            "themselves (make services-restart for JARVIS's own services)")
# Refused for in-place tasks, which share the user's checkout
IN_PLACE_GIT = ("commit", "push", "reset", "checkout", "switch", "restore", "stash", "clean", "rebase", "merge",
                "pull", "cherry-pick", "revert", "am", "branch", "tag", "worktree", "update-ref", "gc", "prune", "init")
# Lines cat prints from a heredoc (a commit message), up to its delimiter alone on a line; group 1 keeps the cat <<
# word. Only cat's: what a heredoc feeds a shell are commands.
_HEREDOC_BODY = re.compile(r"(\bcat[ \t]+<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\2[^\n]*)\n.*?\n[ \t]*\3[ \t]*(?=\n|\)|$)", re.S)


def refused(command: str, in_place: bool = False, cwd: Path | None = None) -> str:
    """Why a task may never run this on the computer, even with a yes, or ''. `in_place`: git writes are the user's.
    `cwd`: where it runs, for reading the scripts it runs."""
    if names_secret(command):
        return "it touches secrets, which stay out of reach even with a yes"
    if seen(command, cwd).secret:
        return "it runs a script that touches secrets, which stay out of reach even with a yes"
    if reads_environment(command):
        return "it reads other processes' environments, where their keys are, which stay out of reach even with a yes"
    if _CONTROLS.search(command):
        return "it touches JARVIS's own controls, which tasks can't reach"
    if _SECRET_SERVICE.search(command):
        return "it reads or changes passwords in the keychain"
    for argv in _simple_commands(command) or []:
        argv = _strip_prefixes(argv)
        if not argv:
            continue
        program, rest = Path(argv[0].lstrip("\\")).name, _options_then(argv[1:])
        if program in _ROOT:
            return f"{program} wants a password typed in a terminal, and tasks have none"
        if (program == "security" and rest and _KEYCHAIN_DUMPS.fullmatch(rest[0])
                or program in ("kwallet-query", "pass") or program == "secret-tool" and set(rest[:1]) & _SECRET_TOOL_USES
                or program == "nmcli" and _nmcli(argv[1:]) == "reads the keychain"):
            return "it reads or changes passwords in the keychain"
        if platform.LINUX and (why := _user_bus(program, argv[1:])):
            return why
    if in_place and (found := in_place_git(command)):
        return (f"git {found[0]} changes the user's own checkout, which in-place tasks leave to them; "
                "list it in your summary instead")
    return ""


def in_place_git(command: str) -> list[str]:
    """Each git subcommand here that changes the user's checkout ('commit', 'push'...), in order.

    An amend, which rewrites a commit of theirs, is 'commit --amend' (as is its abbreviation), unlike a new commit."""
    if "git" not in command:  # most of a task's commands: skip the parsing
        return []
    if "<<" in command:
        command = _HEREDOC_BODY.sub(r"\1", command)  # a commit message's lines aren't commands, nor its apostrophes quotes
    if (pieces := _simple_commands(command)) is None:  # unbalanced quotes: every git word, operators split off
        words = re.split(r"[\s;&|()`]+", command)
        pieces = [words[i:] for i, w in enumerate(words) if Path(w).name == "git"]
    found = []
    for argv in pieces:
        argv = _strip_prefixes(argv)
        if not argv or Path(argv[0].lstrip("\\")).name != "git":
            continue
        sub = _options_then(argv[1:], {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
        if sub and sub[0] in IN_PLACE_GIT:
            found.append("commit --amend" if sub[0] == "commit" and any(a.startswith("--am") for a in sub) else sub[0])
    return found


def _user_bus(program: str, args: list[str]) -> str:
    """Why a task can't run this on Linux for want of the user's D-Bus, or ''."""
    if program == "make" and (target := next((a for a in args if _SERVICE_TARGETS.fullmatch(a)), "")):
        return f"JARVIS's services are started and stopped by the user, not tasks: ask them to run make {target}"
    if "--user" not in args:
        return ""
    if program == "systemd-run":
        return USER_BUS
    if program == "systemctl":
        sub = _options_then(args, _SUB_VALUES["systemctl"])
        if sub and sub[0] not in _READ_ONLY_SUBCOMMANDS["systemctl"]:
            return USER_BUS
    return ""
