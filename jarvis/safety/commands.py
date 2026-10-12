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
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.core import platform
from jarvis.core.config import CLAUDE_CONFIG_DIR, DATA_DIR, ROOT, SECRETS_DIR

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


def secret_file_names(char: str = ".", pem: bool = True) -> str:
    """Secret file names as one regex alternation.

    `char` is what a wildcard matches ('[^/]' stays within one path component). `pem=False` omits .pem files.
    """
    keys = "pem|key" if pem else "key"
    return (rf"\.env(\.{char}*)?|{char}+\.({keys}|p8|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)|\.netrc|\.npmrc"
            rf"|\.pypirc|\.git-credentials|credentials(\.json)?|{char}*client_secret{char}*\.json|tokens?\.json")


SECRET_FILES = re.compile(secret_file_names(), re.IGNORECASE)
# Same files as globs, for Claude Code's Read deny rules
SECRET_PATTERNS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p8", "**/*.p12", "**/*.pfx", "**/id_rsa",
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


def names_secret(text: str) -> bool:
    """Whether text names a secret: a secrets folder, .env file, key or login."""
    return bool(_SECRET_NAMES.search(text))


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

    Tolerates punctuation, filler and "O.K."; anything less than a clear yes, or a lone "thank you", is not one.
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
            agreed, i = True, i + len(phrase)
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

# A line is split into simple commands at ; & | && || ( ) and newlines, each judged by program and args.
# Code run from a string (shell -c, eval, interpreters) can't be judged, so it is risky itself.
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
    "eval": "runs code it can't see", "watch": "runs code it can't see", "script": "runs code it can't see",
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
_INLINE_CODE = {"python": {"-c", "-"}, "node": {"-e", "--eval", "-p", "--print"}, "perl": {"-e", "-E"}, "ruby": {"-e"},
                "php": {"-r"}, "deno": {"eval"}}
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


def _strip_prefixes(argv: list[str]) -> list[str]:
    """argv minus wrappers (nohup, env, VAR=value, xargs -I {}, timeout 5, xcrun)."""
    while argv and (argv[0] in _PREFIXES or re.fullmatch(r"\w+=.*", argv[0])):
        argv = argv[1:]
        while argv and (argv[0].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", argv[0])):
            argv = argv[2:] if argv[0] in _PREFIX_VALUES else argv[1:]
    if argv and Path(argv[0]).name == "xcrun":  # the tool xcrun finds is the command
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
        return next((why for i in runs if (why := _piece(args[i + 1 :]))), "")
    if program in _SHELLS and (not _options_then(args) or any(re.fullmatch(r"-\w*c\w*", a) for a in args)):
        return "runs code it can't see"
    if program in _INLINE_CODE and (not args or flags & _INLINE_CODE[program]):
        return "runs code it can't see"
    return ""


def _simple_commands(command: str) -> list[list[str]] | None:
    """Every simple command as argv, including those in $(...) and `...`; None on unbalanced quotes.

    Redirection targets are dropped, not treated as arguments.
    """
    found: list[list[str]] = []
    command = _FD_NUMBER.sub(" ", command)  # the 2 in 2>/dev/null isn't an argument
    for inner in _SUBSTITUTIONS.findall(command):  # these run even inside double quotes
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
            redirect = True  # next word is a redirect target
        elif token and set(token) <= set(";&|()"):
            if argv:
                found.append(argv)
            argv = []
        else:
            argv.append(token)
    return found


def risky_pieces(command: str, own: "Own | None" = None) -> list[tuple[list[str], str]]:
    """[(unwrapped argv, why)] for each risky simple command, in order; [] if safe.

    With `own`, a task's own deletes/kills are exempt. Whole-line risks (dotfile clobber, unparsable) come as ([], why).
    """
    if _CLOBBER_DOTFILE.search(command):
        return [([], "overwrites a settings file")]
    if (pieces := _simple_commands(command)) is None:
        return [([], "can't be checked")]
    if why := next((why for t in _REDIRECT_TARGETS.findall(command) if (why := _starts_up(t))), ""):
        return [([], why)]  # > /etc/hosts
    # cd ~/.config/systemd/user && cp x.service .: every write there counts
    there = any(p[0] in ("cd", "pushd") and len(p) > 1 and _starts_up(p[1]) for p in map(_strip_prefixes, pieces) if p)
    if there and _REDIRECT_TARGETS.search(command):
        return [([], "changes the system")]
    found, mine = [], None
    for argv in pieces:
        why = _piece(argv)
        if not why and there and Path((_strip_prefixes(argv) or argv)[0]).name in _WRITES_THERE:
            why = "changes the system"
        if not why:
            continue
        if own and (mine := mine or _Mine(command, pieces, own)).covers(argv):
            continue
        found.append((_strip_prefixes(argv) or argv, why))
    return found


def _risk(command: str, own: "Own | None" = None) -> tuple[str, str]:
    """(why, program) for the first risky piece; ('', '') if safe."""
    if not (found := risky_pieces(command, own)):
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


def risky(command: str, own: "Own | None" = None) -> str:
    """Why a command needs a yes ('deletes files', ...), or ''. Errs toward asking: unreadable means risky.

    `own`: a background task's deletes in its folders and kills of its processes need no yes.
    """
    return _risk(command, own)[0]


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


def describe(command: str, own: "Own | None" = None) -> str:
    """'installs software (brew install)', from the command itself (never a model's summary); '' if safe."""
    why, what = _risk(command, own)
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
    "merge": {"merge"}, "create": {"open"}, "open": {"open"}, "raise": {"open"},
}
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
    return {act for w in words for act in _VERBS.get(w, ())}


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


def refused(command: str, in_place: bool = False) -> str:
    """Why a task may never run this on the computer, even with a yes, or ''. `in_place`: git writes are the user's."""
    if names_secret(command):
        return "it touches secrets, which stay out of reach even with a yes"
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
        if in_place and program == "git":
            sub = _options_then(argv[1:], {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
            if sub and sub[0] in IN_PLACE_GIT:
                return (f"git {sub[0]} changes the user's own checkout, which in-place tasks leave to them; "
                        "list it in your summary instead")
    return ""


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
