"""Scanning text for hidden instructions before JARVIS keeps it or loads it: prompt injection aimed at the assistant,
and invisible or unusual Unicode that could hide it.

  scan(text, source)   a Scan: the text with invisible characters stripped (`clean`), what was found (`findings`),
                       and whether to flag it (`suspicious`, said in a sentence by `reason`)
  clean(text)          just the stripping, for what's kept whatever it says (the user's own words in the panel)
  reveal(text)         the stripping, with what tag characters spell out shown instead, for a model that judges it
  scan_file(path)      scan() of a file's text, as instructions (a CLAUDE.md, a skill)

Used before anything is written to long-term memory, the to-do list or the project list: tasks' and the learning
review's suggested edits (jarvis.tasks.data_edits) are flagged for the user's yes rather than applied, and the voice
brain's own saves are refused with what was found, for it to tell the user. Code tasks scan the project files they load
(jarvis.tasks.project_claude): CLAUDE.md, AGENTS.md, subagents, skills and slash commands, leaving out any that look
tampered with and telling the user. It's here, apart from all of them, for anything else that vets text, such as
installed skills.

What counts:

  invisible   zero-width characters, bidirectional controls, Unicode tag characters (which can spell out hidden
              ASCII: decoded into the finding), variation selectors and other format characters. Always stripped.
              Bidirectional controls and tag characters are flagged; a stray zero-width space from a copy and paste
              isn't, but several are.
  unusual     private-use characters, which render as nothing or as anything a font likes. Flagged, not stripped.
  injection   phrases that try to change the assistant's rules ("ignore previous instructions", "you are now",
              "bypass the sandbox", "auto-approve"), keep things from the user ("don't tell the user", or the user
              by their configured name, as it was when the service started), get data sent
              away ("send the API key to ..."), or fake the start of a system or assistant message. A CLAUDE.md or a
              skill is meant to instruct the assistant, so with source="instructions" only the plain attacks count, not
              text that merely talks to it.

It errs towards flagging: the cost of a false alarm is one question to the user.
"""

import functools
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.core import profile
from jarvis.core.config import ASSISTANT_NAME

DATA, INSTRUCTIONS = "data", "instructions"  # what the text is: something kept, or something meant to instruct
EXCERPT = 80  # characters of the text around a finding, quoted in it
ZERO_WIDTH_FLAGGED = 3  # this many zero-width characters in one text look like a hidden encoding

# ── Characters ──────────────────────────────────────────────────

_ZERO_WIDTH = set("​‌‍⁠⁡⁢⁣⁤﻿᠎͏ᅟᅠㅤﾠ­")
_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
_TAGS = range(0xE0000, 0xE0080)
_VARIATION = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))
_PRIVATE = (range(0xE000, 0xF900), range(0xF0000, 0x110000))
_KEPT_FORMAT = set("\n\r\t")


def _kind(ch: str) -> str | None:
    """What sort of hidden character `ch` is, or None for an ordinary one."""
    cp = ord(ch)
    if ch in _BIDI:
        return "bidi"
    if cp in _TAGS:
        return "tag"
    if ch in _ZERO_WIDTH:
        return "zero-width"
    if any(cp in r for r in _VARIATION):
        return "variation"
    if any(cp in r for r in _PRIVATE):
        return "private"
    if ch not in _KEPT_FORMAT and (category := unicodedata.category(ch)) in ("Cf", "Cc", "Co", "Cs"):
        return "control" if category == "Cc" else "format"
    return None


_STRIPPED = {"bidi", "tag", "zero-width", "variation", "format", "control"}


def clean(text: str) -> str:
    """The text without invisible characters (zero-width, bidirectional controls, tags, variation selectors and
    other format or control characters, but for newlines and tabs)."""
    return "".join(ch for ch in text if _kind(ch) not in _STRIPPED)


def reveal(text: str) -> str:
    """clean(text), but with what each run of Unicode tag characters spells out shown where it was, as
    [hidden text: ...], for a reader that judges it (jarvis.safety.injection)."""
    out, hidden = [], []
    for ch in text + " ":  # the space ends a run at the very end
        kind = _kind(ch)
        if kind == "tag":
            if 0x20 <= ord(ch) - 0xE0000 < 0x7F:
                hidden.append(chr(ord(ch) - 0xE0000))
            continue
        if hidden:
            out.append(f"[hidden text: {''.join(hidden)}]")
            hidden = []
        if kind not in _STRIPPED:
            out.append(ch)
    return "".join(out)[:-1]


# ── Phrases ─────────────────────────────────────────────────────


def _names(name: str) -> list[str]:
    """Patterns for a configured name, whole and by its first word ("Sam Lee", "Sam"), any whitespace between the
    words; none for "the user", the default with no name set, whose first word would match far too much."""
    words = name.split()
    if not words or name.strip().lower() == "the user":
        return []
    return list(dict.fromkeys([r"\s+".join(map(re.escape, words)), re.escape(words[0])]))


_RULES = r"(instructions|instruction|rules|prompts?|directions|guidelines|directives|guardrails|polic(y|ies))"
_SECRET = (r"(api[\s_-]?keys?|access\s+tokens?|tokens?|passwords?|passcodes?|secrets?|credentials?|private\s+keys?|"
           r"ssh\s+keys?|\.env(\s+file)?|cookies|session\s+logs?|system\s+prompt|memory\.md|conversation\s+logs?)")
_WHERE = r"(https?://|www\.|[\w.+-]+@[\w-]+\.\w|\b[\w-]+\.(com|net|org|io|xyz|ru|cn|site|app|dev)\b|webhook|pastebin|ngrok)"


def compile_patterns(user: str, assistant: str) -> dict[str, list[tuple[str, re.Pattern]]]:
    """The phrase patterns for each source, DATA and INSTRUCTIONS, with the user's and the assistant's names among the
    words for each ("don't tell Sam", "Dear JARVIS"), as well as the general ones ("the user", "the assistant")."""
    named = _names(assistant)
    bot = "(" + "|".join([r"ai|assistant|agent|bot|model|llm|claude|chatgpt|gpt", *named]) + ")"
    who = "(" + "|".join([r"the\s+user|the\s+owner|your\s+user|your\s+owner|the\s+human", *_names(user)]) + ")"
    owned = "".join(rf"|{n}'?s?\s+" for n in named)  # "bypass JARVIS's safety"
    attacks = [  # (what it does, the pattern): plain attacks, wherever they are, CLAUDE.md and skills included
        ("tries to override the assistant's rules",
         rf"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(of\s+)?(the\s+|your\s+|my\s+|these\s+|those\s+)?"
         rf"(previous|prior|above|earlier|preceding|former|original|existing|system|other)\s+{_RULES}\b|"
         rf"\b(ignore|disregard|forget)\s+(all|any|everything)\s+(of\s+)?(your|the|you\s+were\s+told)\b(\s+{_RULES})?"),
        ("tries to give the assistant new rules",
         rf"\b(new|updated|revised|real|true|actual|secret|hidden)\s+(system\s+)?{_RULES}\s*(:|follow\b|for\s+you)"),
        ("tries to turn safety checks off",
         r"\b(bypass|disable|turn\s+off|switch\s+off|circumvent|evade|get\s+around|deactivate)\s+(the\s+|your\s+|"
         rf"all\s+|any\s+{owned})?(safety|security|sandbox(ing)?|guard(rails?)?|gate|content\s+filters?|"
         r"restrictions?|content\s+polic\w+)\b"),
        ("tries to keep something from the user",
         rf"\b(do\s*n[o']?t|never)\s+(tell|inform|notify|alert)\s+{who}\b(?!\s+to\b)|"
         rf"\b(do\s*n[o']?t|never)\s+(mention|reveal|show|report)\s+(this|it|that|these|any\s+of\s+this)\s+to\s+{who}|"
         rf"\bwithout\s+(telling|informing|notifying|alerting)\s+{who}\b|"
         rf"\b(silently|secretly|covertly|without\s+{who}\s+knowing)\s+(run|send|delete|approve|forward|upload|"
         r"execute|install|post|email|copy|exfiltrate)\b"),
        ("asks for data to be sent away",
         rf"\b(send|post|upload|forward|email|e-mail|exfiltrate|transmit|curl|wget)\s+(?:\S+\s+){{0,5}}?{_SECRET}\b"
         rf"[^.\n]{{0,80}}?{_WHERE}"),
        ("asks the assistant to reveal its instructions",
         r"\b(reveal|print|show|output|repeat|dump|leak|disclose)\s+(your|the)\s+(full\s+|entire\s+|hidden\s+)?"
         r"(system\s+prompt|hidden\s+instructions|initial\s+instructions|instructions\s+above)"),
    ]
    # Text kept as data (memory, to-dos, projects) shouldn't talk to the assistant or about its rules at all
    data_only = [
        ("tries to approve commands without asking",
         r"\b(auto[\s-]?approve|always\s+approve|approve\s+(all|every|any)\s+(commands?|requests?|actions?)|"
         r"(run|approve|execute|install|delete|remove|push|send|commands?)\b[^.\n]{0,40}\b(without|no\s+need\s+for)\s+"
         r"(asking|approval|confirmation|permission|a\s+yes)\b)"),
        ("fakes a system or assistant message",
         r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?(INST|SYS)\]|<</?SYS>>|^\s*#{1,6}\s*(system|assistant)\s*"
         r"(prompt|message|instructions?)?\s*:?\s*$|\bBEGIN\s+(SYSTEM|ADMIN)\s+(PROMPT|INSTRUCTIONS|MESSAGE)\b|"
         r"</?(system|system-prompt|system_prompt|admin|developer)>|^\s*(system|assistant)\s*:"),
        ("tries a known jailbreak",
         r"\b(jailbreak|jailbroken|DAN\s+mode|developer\s+mode\s+enabled|do\s+anything\s+now)\b"),
        ("is addressed to the assistant",
         rf"\b(attention|note|message|instructions?|important|reminder)\s+(to|for)\s+(the\s+|any\s+)?{bot}s?\b|"
         rf"^\W*(dear|hey|hi|hello|attention|important)[\s,]+{bot}\b|\b{bot}\s*[,:]\s*(you\s+(must|should|will)|"
         r"ignore|always|never|from\s+now\s+on)"),
        ("tries to change who the assistant is",
         r"\b(you\s+are\s+now|from\s+now\s+on,?\s+you\s+(are|will|must|should)|act\s+as\s+(an?\s+)?(unrestricted|"
         r"different|new)|pretend\s+(to\s+be|you\s+are)|roleplay\s+as|your\s+new\s+(role|persona|identity|name)\s+is)"),
        ("tries to turn safety checks off",
         r"\b(bypass|disable|turn\s+off|skip|ignore|circumvent)\s+(the\s+|your\s+|all\s+|any\s+)?"
         r"(approvals?|permissions?|confirmations?|checks?|scan(ning|s|ner)?)\b"),
    ]
    return {
        INSTRUCTIONS: [(what, re.compile(p, re.IGNORECASE | re.MULTILINE)) for what, p in attacks],
        DATA: [(what, re.compile(p, re.IGNORECASE | re.MULTILINE)) for what, p in attacks + data_only],
    }


@functools.lru_cache(maxsize=8)
def _patterns(user_name: str) -> dict[str, list[tuple[str, re.Pattern]]]:
    return compile_patterns(user_name, ASSISTANT_NAME)


def _compiled() -> dict[str, list[tuple[str, re.Pattern]]]:
    """The patterns for the name of the person JARVIS works for now (jarvis.core.profile), compiled once each."""
    return _patterns(profile.name())


# ── Scanning ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    kind: str  # "invisible", "unusual" or "injection"
    what: str  # in a few words: "tries to override the assistant's rules", "2 bidirectional controls"
    excerpt: str = ""  # the text it was found in, or what tag characters spell out
    flagged: bool = True  # False for what's only stripped: a stray zero-width space, an emoji's variation selector

    def __str__(self) -> str:
        return f"{self.what}: “{self.excerpt}”" if self.excerpt else self.what


@dataclass
class Scan:
    clean: str  # the text without invisible characters
    findings: list[Finding] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        """Whether to flag it to the user rather than keep or load it quietly."""
        return any(f.flagged for f in self.findings)

    def reason(self) -> str:
        """What was found that's flagged, in a sentence: 'it tries to override the assistant's rules: “…”; it has 2
        bidirectional controls'."""
        return "; ".join(("it " if f.kind == "injection" else "it has ") + str(f) for f in self.findings if f.flagged)


def _excerpt(text: str, start: int, end: int) -> str:
    pad = max(0, (EXCERPT - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


_NAMES = {"bidi": "bidirectional control", "tag": "Unicode tag character", "zero-width": "zero-width character",
          "variation": "variation selector", "format": "invisible format character", "control": "control character",
          "private": "private-use character"}
# How many of a kind it takes to flag it: a few zero-width spaces come with copied text, and emoji carry variation
# selectors, but many can spell out a hidden message. Those in the supplement (beyond U+E0100) are never in emoji.
_FLAG_AT = {"zero-width": ZERO_WIDTH_FLAGGED, "variation": 8}


def scan(text: str, source: str = DATA) -> Scan:
    """Scan text for hidden instructions; `source` is DATA for what's kept (memory, to-dos, projects) or
    INSTRUCTIONS for what's meant to instruct the assistant (CLAUDE.md, skills), where only plain attacks count."""
    if source not in (compiled := _compiled()):
        raise ValueError(f"source must be {DATA} or {INSTRUCTIONS}.")
    counts: dict[str, int] = {}
    kept, hidden, supplement = [], [], False
    for ch in text:
        if kind := _kind(ch):
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "tag" and 0x20 <= ord(ch) - 0xE0000 < 0x7F:
                hidden.append(chr(ord(ch) - 0xE0000))
            supplement |= ord(ch) >= 0xE0100 and kind == "variation"
            if kind in _STRIPPED:
                continue
        kept.append(ch)
    stripped = "".join(kept)  # clean(text), from the same pass
    findings = []
    for kind, n in counts.items():
        spelled = "".join(hidden) if kind == "tag" else ""
        flagged = n >= _FLAG_AT.get(kind, 1) or (kind == "variation" and supplement)
        findings.append(Finding("unusual" if kind == "private" else "invisible", f"{n} {_NAMES[kind]}{'s' * (n > 1)}",
                                f"hidden text: {spelled[:EXCERPT]}" if spelled.strip() else "", flagged))
    # The words as they'd read once invisible characters are gone, and fullwidth or styled letters made plain; with
    # any text the tag characters spelled out
    plain = unicodedata.normalize("NFKC", stripped) + ("\n" + "".join(hidden) if hidden else "")
    for what, pattern in compiled[source]:
        if any(f.what == what for f in findings):
            continue
        if m := pattern.search(plain):
            findings.append(Finding("injection", what, _excerpt(plain, m.start(), m.end())))
    return Scan(stripped, findings)


def scan_file(path: Path, source: str = INSTRUCTIONS) -> Scan:
    """scan() of a file's text (read as UTF-8, any bad bytes replaced)."""
    return scan(Path(path).read_bytes().decode("utf-8", errors="replace"), source)


class Suspicious(ValueError):
    """Text that looks like hidden instructions, refused with what was found."""

    def __init__(self, found: Scan, what: str = "it") -> None:
        super().__init__(f"Not saved: {what} looks like hidden instructions ({found.reason()}).")
        self.scan = found


def vetted(text: str, what: str = "it", source: str = DATA) -> str:
    """The text, cleaned of invisible characters, if nothing about it is suspicious; else raises Suspicious."""
    found = scan(text, source)
    if found.suspicious:
        raise Suspicious(found, what)
    return found.clean
