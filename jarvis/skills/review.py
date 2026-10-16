"""Script reviews the user hears before enabling a skill that has scripts.

  scan       fixed patterns over scripts (network, deletes, secrets, env, subprocess, installs, persistence, sudo,
             obfuscation, binaries, !`command`) plus hidden-instruction checks on markdown (safety.scan)
  summarise  one tool-less SKILL_REVIEW_MODEL call over the fenced (untrusted) files; reply parsed as JSON only
  run        both, pinned to home.digest: approval covers exactly the files reviewed
"""

import json
import re
import time
from pathlib import Path

from jarvis import prompts
from jarvis.core import profile
from jarvis.core.config import SKILL_REVIEW_MODEL
from jarvis.safety import scan as scan_text
from jarvis.safety.commands import ENV_TEMPLATES, names_secret
from jarvis.skills import home

MAX_CHARS = 60_000  # total chars sent; beyond this files are omitted and marked partial
MAX_FILE = 20_000  # chars per file
CALL_TIMEOUT = 180
VERDICTS = ("low", "medium", "high")

# (spoken label, pattern)
PATTERNS: list[tuple[str, re.Pattern]] = [(label, re.compile(rx, re.IGNORECASE)) for label, rx in [
    ("reaches the network", r"\b(curl|wget|nc|ncat|telnet|ssh|scp|sftp|rsync|ftp)\s|\brequests\.(get|post|put|patch|"
                            r"delete|request|Session)|\burllib|\bhttp\.client|\bhttpx\b|\baiohttp\b|\bfetch\(|"
                            r"XMLHttpRequest|\bsocket\.|\bnet\.(connect|createConnection)|WebSocket|https?://"),
    ("deletes files", r"\brm\s+-[a-z]*[rf]|\brmdir\b|shutil\.rmtree|os\.(remove|unlink|rmdir)|\.unlink\(|unlinkSync|"
                      r"rmSync|fs\.rm\b|\bshred\b|find\s.*-delete"),
    ("reads secrets or credentials", r"security\s+(find|dump|export)-|Keychains?\b|\.aws/|\.ssh/|\bid_(rsa|ed25519)\b|"
                                     r"\.netrc|\.git-credentials|\bkeyring\b|\bsecret-tool\b|kwallet|"
                                     rf"\.env\b(?!\.({'|'.join(ENV_TEMPLATES)})(?![\w.-]))|"  # not .env.example
                                     r"\.password-store|\bpass\s+show\b|\.local/share/keyrings|org\.freedesktop\.secrets|"
                                     r"\bSecret\.Service\b|\blibsecret\b|--show-secrets"),
    ("reads environment variables, which can hold keys", r"os\.environ|\bgetenv\(|process\.env|\bENV\[|"
                                                         r"\$\{?\w*(TOKEN|KEY|SECRET|PASSWORD)"),
    ("runs other programs", r"\bsubprocess\b|os\.(system|popen|exec\w*)\(|child_process|execSync|\bspawn\(|"
                            r"\beval\(|\bexec\(|Popen|\bosascript\b"),
    ("downloads and runs code", r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b|\bpip3?\s+install|\bnpm\s+(i|install)\b|"
                                r"\bnpx\s|\buvx\s|\bbrew\s+install|\bgo\s+install|\bcargo\s+install|\bgem\s+install|"
                                r"\b(apt|apt-get|dnf|yum|zypper|snap|flatpak)\s+(-\S+\s+)*install\b|\bapk\s+add\b|"
                                r"\b(pacman|yay|paru)\s+-S|\b(dpkg\s+-i|rpm\s+-[iU])"),
    ("starts at login or changes the system", r"\blaunchctl\b|LaunchAgents|LaunchDaemons|\bcrontab\b|\.zshrc|\.bashrc|"
                                              r"\.bash_profile|\.zprofile|defaults\s+write|\bchown\b|\bpmset\b|"
                                              r"\bnetworksetup\b|\bsystemsetup\b|"
                                              # Linux: systemd units and timers, autostart entries, login scripts
                                              r"\bsystemctl\b|\bsystemd-run\b|\bloginctl\s+enable-linger|"
                                              r"systemd/(user|system)\b|\.config/autostart|\bWantedBy=|"
                                              r"\[Install\]|X-GNOME-Autostart|/\.profile\b|['\"]\.profile['\"]|\.xprofile|\.xinitrc|"
                                              r"\.pam_environment|environment\.d/|\bupdate-rc\.d\b|\bchkconfig\b|"
                                              r"\bgsettings\s+set\b|\bdconf\s+(write|load)\b|\bnmcli\b|\biptables\b|"
                                              r"\bnft\s|\bufw\s|\bmodprobe\b|\bsysctl\s+-w|"
                                              # writes to /etc
                                              r"(>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b|\bln\b|\bsed\s+-i)[^\n;|&]*\s['\"]?/etc/|"
                                              r"open\(\s*['\"]/etc/[^'\"]*['\"]\s*,\s*['\"][wax]|"
                                              r"(write_text|writeFileSync|appendFileSync)\(\s*['\"]/etc/"),
    ("uses sudo", r"\b(sudo|doas|pkexec|run0)\b"),
    ("hides what it runs", r"base64\s+(-d|--decode|-D)\b|b64decode|\batob\(|fromCharCode|(\\x[0-9a-f]{2}){12,}|"
                           r"[A-Za-z0-9+/]{240,}={0,2}"),
]]

def system() -> str:
    return prompts.load("skill_review", user_name=profile.name())


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan(where: Path) -> list[str]:
    """Pattern findings, each like 'reaches the network (scripts/get.py)'."""
    hits: dict[str, list[str]] = {}
    for rel in home.files(where):
        path = where / rel
        if path.is_symlink():
            continue
        markdown, code = rel.endswith(".md"), rel == home.SKILL_FILE or home.is_script(path)
        if not (markdown or code):
            continue  # documents and data aren't executed
        text = _text(path)
        if markdown and text is not None:
            # Markdown the model reads: check for hidden instructions or invisible text
            for finding in scan_text.scan(text, scan_text.INSTRUCTIONS).findings:
                if finding.flagged:
                    hits.setdefault(f"looks like hidden instructions: it {'has ' * (finding.kind != 'injection')}"
                                    f"{finding.what}", []).append(rel)
        if not code:
            continue
        if text is None:
            hits.setdefault("holds compiled programs, which can't be read", []).append(rel)
            continue
        if rel == home.SKILL_FILE:
            if home.INLINE.search(text):
                hits.setdefault("has commands in its SKILL.md meant to run as it loads, which JARVIS doesn't run",
                                []).append(rel)
            if names_secret(text):
                hits.setdefault("mentions secrets or credentials in its instructions", []).append(rel)
            continue  # SKILL.md prose isn't code; skip the script patterns
        for label, pattern in PATTERNS:
            if pattern.search(text):
                hits.setdefault(label, []).append(rel)
    return [f"{label} ({', '.join(found[:3])}{f' and {len(found) - 3} more' if len(found) > 3 else ''})"
            for label, found in hits.items()]


def _fenced(where: Path) -> tuple[str, bool]:
    """Files fenced for the model (SKILL.md, scripts, rest), and whether any were cut to fit MAX_CHARS."""
    listed = home.files(where)
    order = [home.SKILL_FILE] + [f for f in listed if f != home.SKILL_FILE and home.is_script(where / f)]
    order += [f for f in listed if f not in order]
    parts, used, partial = [], 0, False
    for rel in order:
        path = where / rel
        text = None if path.is_symlink() else _text(path)
        if text is None:
            parts.append(f'<file path="{rel}">(not text: {path.stat().st_size if path.exists() else 0} bytes)</file>')
            continue
        if len(text) > MAX_FILE:
            text, partial = text[:MAX_FILE] + "\n… (cut short)", True
        if used + len(text) > MAX_CHARS:
            partial = True
            parts.append(f'<file path="{rel}">(left out: too much to read in one review)</file>')
            continue
        used += len(text)
        parts.append(f'<file path="{rel}">\n{text.replace("</file>", "</ file>")}\n</file>')
    return "\n".join(parts), partial


def parse(reply: str) -> dict:
    """Parse the model's JSON review; unknown verdicts become "high". Raises ValueError if unreadable."""
    found = re.search(r"\{.*\}", reply or "", re.DOTALL)
    try:
        got = json.loads(found[0]) if found else None
    except ValueError:
        got = None
    if not isinstance(got, dict) or not (summary := home.one_line(got.get("summary"))):
        raise ValueError("the review's reply wasn't readable")
    risks = [home.one_line(r) for r in got.get("risks") or [] if home.one_line(r)][:8]
    verdict = str(got.get("verdict") or "").strip().lower()
    return {"summary": summary[:800], "risks": [r[:200] for r in risks],
            "verdict": verdict if verdict in VERDICTS else "high"}


async def summarise(where: Path, name: str) -> tuple[dict, bool]:
    """(review, partial). Raises RuntimeError (call failed), ValueError (unreadable) or TimeoutError."""
    from jarvis.brain.sdk import ask

    files, partial = _fenced(where)
    prompt = f"The skill is called {name}. Its files:\n\n{files}"
    reply, _ = await ask(prompt, system(), SKILL_REVIEW_MODEL, timeout=CALL_TIMEOUT)
    return parse(reply), partial


async def run(name: str, summariser=None) -> dict:
    """Scan + summarise a skill and store the review pinned to its digest. Raises ValueError if the summary fails or
    files change mid-review. `summariser` is for tests."""
    skill = home.get(name)
    where, before = skill.folder, skill.digest
    try:
        found, partial = await (summariser or summarise)(where, skill.name)
    except (RuntimeError, ValueError, TimeoutError) as e:
        raise ValueError(f"The review of {skill.name}'s scripts didn't work ({e}); try again in a minute.") from None
    after = home.digest(where)
    if after != before:
        raise ValueError(f"The skill {skill.name}'s files changed while it was being reviewed; review it again.")
    review = found | {"flags": scan(where), "partial": partial, "digest": after, "scripts": skill.scripts,
                      "model": SKILL_REVIEW_MODEL, "at": time.time()}
    home.set_review(skill.name, review)
    return review


def current(skill: home.Skill) -> dict | None:
    """The stored review if it matches the current digest, else None."""
    return skill.review if skill.review and skill.review.get("digest") == skill.digest else None


def said_flags(flags: list[str]) -> str:
    """Scan flags for speech, without file names."""
    return "; ".join(re.sub(r" [(].*", "", f) for f in flags)


def spoken(name: str, review: dict) -> str:
    """Spoken review read out before asking for approval."""
    scripts = review.get("scripts") or []
    text = f"The skill {name} has {len(scripts)} script{'s' * (len(scripts) != 1)}. {review['summary']}"
    if review.get("risks"):
        text += f" Risks: {'; '.join(review['risks'])}."
    if flags := review.get("flags"):
        text += f" A scan found it {said_flags(flags)}."
    text += f" Rated {review.get('verdict', 'high')} risk."
    if review.get("partial"):
        text += " Some files were too long to read in full."
    return text.replace("..", ".")
