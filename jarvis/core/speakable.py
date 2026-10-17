"""Text made fit to read aloud: markdown, fences, HTML and link targets stripped. Used for audio files
(jarvis.tools.audio) and for lookup answers said in a conversation (jarvis.brain.side)."""

import re

_FENCE = re.compile(r"^\s*(```|~~~).*$", re.MULTILINE)
_HTML = re.compile(r"<[^>\n]{1,200}>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_URL = re.compile(r"\b(?:https?://|www\.)(?:www\.)?([A-Za-z0-9.-]*[A-Za-z0-9])(?:[/?#:][^\s)>\]]*[^\s)>\].,;:!?'\"])?")
_EMPHASIS = re.compile(r"(\*\*|\*|~~|`)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\3(?!\w)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+•▪◦]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?")
_RULE = re.compile(r"^\s*([-*_=])(\s*\1){2,}\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_ENDS = ".!?…:;,"


def _closed(line: str) -> str:
    """Add a full stop if the line has no ending punctuation."""
    return line if not line or line[-1] in _ENDS + "\"'”’)" else line + "."


def speakable(text: str) -> str:
    """Strip markdown, fences, HTML and link targets (bare URLs become the site name) for reading aloud.

    Headings, list items and table rows become sentences; paragraphs are blank-line separated; other newlines
    (e.g. from PDFs) become spaces."""
    text = _FENCE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _HTML.sub(" ", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(lambda m: m[1].removeprefix("www.").rstrip("."), text)
    paragraphs, lines = [], []

    def end_paragraph() -> None:
        if lines:
            paragraphs.append(" ".join(lines))
            lines.clear()

    for raw in text.split("\n"):
        line = _QUOTE.sub("", raw).strip()
        if not line or _RULE.match(line) or _TABLE_RULE.match(line):
            end_paragraph()
            continue
        if heading := _HEADING.match(line):
            end_paragraph()
            paragraphs.append(_closed(_unmarked(heading[1])))
            continue
        if item := _ITEM.match(line):
            line = _closed(_unmarked(item[1]))
        elif line.startswith("|") and line.endswith("|"):
            line = _closed(", ".join(c.strip() for c in line.strip("|").split("|") if c.strip()))
        else:
            line = _unmarked(line)
        lines.append(line)
    end_paragraph()
    return "\n\n".join(p for p in (re.sub(r"\s+", " ", p).strip() for p in paragraphs) if p)


def _unmarked(line: str) -> str:
    for _ in range(3):  # nested: **_both_**
        line = _EMPHASIS.sub(lambda m: m[2] if m[1] else m[4], line)
    return line


def asks_question(text: str) -> bool:
    """True if `text` ends with a question."""
    return text.rstrip().rstrip("\"')]").endswith("?")
