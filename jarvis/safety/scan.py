"""Scan text JARVIS keeps (memory, to-dos, projects) or loads (CLAUDE.md, skills) for hidden instructions.

  scan / scan_file   Scan(clean, findings); `suspicious` and `reason()` say whether and why to flag
  clean / reveal     strip invisible chars; reveal also decodes tag characters for a judging model
  vetted             clean text, or raises Suspicious

Findings: invisible (zero-width, bidi, tag, variation, format; always stripped, flagged by kind/count), unusual
(private-use; flagged, kept), injection (rule overrides, hiding things from the user, exfiltration, fake system
turns). source=INSTRUCTIONS counts only plain attacks. Errs toward flagging: a false alarm costs one question.
"""

import functools
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.config import ASSISTANT_NAME
from jarvis.storage import profile

DATA, INSTRUCTIONS = "data", "instructions"  # stored data vs text meant to instruct
EXCERPT = 80  # chars of context quoted per finding
ZERO_WIDTH_FLAGGED = 3  # this many zero-width chars suggests a hidden encoding

# ── Characters ──────────────────────────────────────────────────

_ZERO_WIDTH = set("​‌‍⁠⁡⁢⁣⁤﻿᠎͏ᅟᅠㅤﾠ­")
_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
_TAGS = range(0xE0000, 0xE0080)
_VARIATION = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))
_PRIVATE = (range(0xE000, 0xF900), range(0xF0000, 0x110000))
_KEPT_FORMAT = set("\n\r\t")


def _kind(ch: str) -> str | None:
    """Hidden-character kind of `ch`, or None."""
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
    """Strip invisible characters: zero-width, bidi, tags, variation selectors, format/control (not \\n \\r \\t)."""
    return "".join(ch for ch in text if _kind(ch) not in _STRIPPED)


def reveal(text: str) -> str:
    """clean(text), with each run of tag characters decoded in place as [hidden text: ...]."""
    out, hidden = [], []
    for ch in text + " ":  # sentinel ends a trailing run
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
    """Regexes for a name, whole and first word; none for the default "the user" (too broad)."""
    words = name.split()
    if not words or name.strip().lower() == "the user":
        return []
    return list(dict.fromkeys([r"\s+".join(map(re.escape, words)), re.escape(words[0])]))


_RULES = r"(instructions|instruction|rules|prompts?|directions|guidelines|directives|guardrails|polic(y|ies))"
_SECRET = (r"(api[\s_-]?keys?|access\s+tokens?|tokens?|passwords?|passcodes?|secrets?|credentials?|private\s+keys?|"
           r"ssh\s+keys?|\.env(\s+file)?|cookies|session\s+logs?|system\s+prompt|memory\.md|conversation\s+logs?)")
_WHERE = r"(https?://|www\.|[\w.+-]+@[\w-]+\.\w|\b[\w-]+\.(com|net|org|io|xyz|ru|cn|site|app|dev)\b|webhook|pastebin|ngrok)"


def compile_patterns(user: str, assistant: str) -> dict[str, list[tuple[str, re.Pattern]]]:
    """Phrase patterns per source (DATA, INSTRUCTIONS), including the user's and assistant's names."""
    named = _names(assistant)
    bot = "(" + "|".join([r"ai|assistant|agent|bot|model|llm|claude|chatgpt|gpt", *named]) + ")"
    who = "(" + "|".join([r"the\s+user|the\s+owner|your\s+user|your\s+owner|the\s+human", *_names(user)]) + ")"
    owned = "".join(rf"|{n}'?s?\s+" for n in named)  # "bypass JARVIS's safety"
    attacks = [  # (what, pattern): plain attacks, flagged in any source
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
    # Stored data shouldn't address the assistant or discuss its rules at all
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
    """Patterns for the current profile's user name, cached."""
    return _patterns(profile.name())


# ── Scanning ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    kind: str  # "invisible", "unusual" or "injection"
    what: str  # e.g. "tries to override the assistant's rules", "2 bidirectional controls"
    excerpt: str = ""  # surrounding text, or decoded tag characters
    flagged: bool = True  # False when only stripped (a stray zero-width space, an emoji variation selector)

    def __str__(self) -> str:
        return f"{self.what}: “{self.excerpt}”" if self.excerpt else self.what


@dataclass
class Scan:
    clean: str  # invisible characters stripped
    findings: list[Finding] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        """Whether to flag it to the user instead of keeping it quietly."""
        return any(f.flagged for f in self.findings)

    def reason(self) -> str:
        """Flagged findings as one sentence: 'it tries to …: “…”; it has 2 bidirectional controls'."""
        return "; ".join(("it " if f.kind == "injection" else "it has ") + str(f) for f in self.findings if f.flagged)


def _excerpt(text: str, start: int, end: int) -> str:
    pad = max(0, (EXCERPT - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


_NAMES = {"bidi": "bidirectional control", "tag": "Unicode tag character", "zero-width": "zero-width character",
          "variation": "variation selector", "format": "invisible format character", "control": "control character",
          "private": "private-use character"}
# Count needed to flag: copied text and emoji carry a few of these legitimately. Supplementary variation selectors
# (U+E0100+) never appear in emoji and are always flagged.
_FLAG_AT = {"zero-width": ZERO_WIDTH_FLAGGED, "variation": 8}


def scan(text: str, source: str = DATA) -> Scan:
    """Scan text for hidden instructions. `source`: DATA (stored text) or INSTRUCTIONS (plain attacks only)."""
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
    stripped = "".join(kept)  # == clean(text)
    findings = []
    for kind, n in counts.items():
        spelled = "".join(hidden) if kind == "tag" else ""
        flagged = n >= _FLAG_AT.get(kind, 1) or (kind == "variation" and supplement)
        findings.append(Finding("unusual" if kind == "private" else "invisible", f"{n} {_NAMES[kind]}{'s' * (n > 1)}",
                                f"hidden text: {spelled[:EXCERPT]}" if spelled.strip() else "", flagged))
    # Visible text, NFKC-normalised (fullwidth/styled letters made plain), plus any decoded tag text
    plain = unicodedata.normalize("NFKC", stripped) + ("\n" + "".join(hidden) if hidden else "")
    for what, pattern in compiled[source]:
        if any(f.what == what for f in findings):
            continue
        if m := pattern.search(plain):
            findings.append(Finding("injection", what, _excerpt(plain, m.start(), m.end())))
    return Scan(stripped, findings)


def scan_file(path: Path, source: str = INSTRUCTIONS) -> Scan:
    """scan() a file's text (UTF-8, bad bytes replaced)."""
    return scan(Path(path).read_bytes().decode("utf-8", errors="replace"), source)


class Suspicious(ValueError):
    """Refused: text looks like hidden instructions."""

    def __init__(self, found: Scan, what: str = "it") -> None:
        super().__init__(f"Not saved: {what} looks like hidden instructions ({found.reason()}).")
        self.scan = found


def vetted(text: str, what: str = "it", source: str = DATA) -> str:
    """Cleaned text if not suspicious; else raises Suspicious."""
    found = scan(text, source)
    if found.suspicious:
        raise Suspicious(found, what)
    return found.clean
