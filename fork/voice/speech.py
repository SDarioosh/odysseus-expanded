"""Fork: an answer as text to be read aloud (docs/fork/voice-assistant.md, "Spoken answers").

The brain writes for the chat window: markdown, lists, transcript links next to facts.
A speaker reads every character, so :func:`to_speech` removes the markdown and the
links and returns the links apart (a screen can show them). :class:`SpeechStream` does
the same while the answer streams, one finished sentence at a time, so text-to-speech
can start before the answer is done.
"""
from __future__ import annotations

import re

# [label](url) and ![alt](url); the label is spoken unless it only names the link
MD_LINK = re.compile(r"!?\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
URL = re.compile(r"<?\bhttps?://[^\s<>()\[\]]+>?")
CODE_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]*)`")
HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
BULLET = re.compile(r"^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+", re.MULTILINE)
QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$",
                        re.MULTILINE)
RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
EMPHASIS = re.compile(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1")
CITATION = re.compile(r"\[\d{1,3}\]")
EMPTY_BRACKETS = re.compile(r"[(\[]\s*[,;:]?\s*[)\]]")
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
REPEATED_PUNCT = re.compile(r"([,;:])\s*(?=[,.;:!?])")
SPACES = re.compile(r"[ \t]+")
LINE_END = re.compile(r"[.!?:;,]$")
# A link's own name: no point reading it out without the link
LINK_WORDS = frozenset({"", "link", "links", "source", "sources", "transcript", "here",
                        "view", "listen", "open", "open transcript", "view transcript",
                        "transcript link", "recording"})
# What is left at the end after the links of "Links: Title: <url> Other: <url>" went:
# phrases that end in a colon with nothing after them
TRAILING_LABELS = re.compile(r"(?:\s*[^.!?:\n]{1,80}:)+\s*\.?\s*$")
# Where a streamed answer may be cut: after a sentence end and a space, or a line break
SENTENCE_END = re.compile(r"[.!?…][\"')\]]*\s+|\n+")


# "Links: [a](u), [b](v)." or "Sources: https://…": the links are kept, the lead-in and
# the link names (conversation titles) are not read out
LINK_LIST = re.compile(
    r"(?i)\b(?:transcript\s+links?|links?|sources?|transcripts?)\s*:\s*"
    r"((?:(?:\[[^\]\n]*\]\([^)\s]+\)|<?https?://[^\s<>()\[\]]+>?)"
    r"(?:\s*(?:,|;|and)\s*)?)+)\s*\.?")


def _link_label(label: str, url: str) -> str:
    text = label.strip()
    if text.lower() in LINK_WORDS or text == url or URL.fullmatch(text):
        return ""
    return text


def _add(links: list[str], url: str) -> None:
    url = url.strip("<>").rstrip(".,;:!?")
    if url and url not in links:
        links.append(url)


def to_speech(text: str) -> tuple[str, list[str]]:
    """``text`` as plain sentences to read aloud, and the links it held (in order, once)."""
    links: list[str] = []
    out = CODE_BLOCK.sub(" ", text or "")

    def md_link(m: re.Match) -> str:
        _add(links, m.group(2))
        return _link_label(m.group(1), m.group(2))

    def bare(m: re.Match) -> str:
        _add(links, m.group(0))
        return ""

    def link_list(m: re.Match) -> str:
        for link in MD_LINK.finditer(m.group(1)):
            _add(links, link.group(2))
        for url in URL.finditer(MD_LINK.sub("", m.group(1))):
            _add(links, url.group(0))
        return ""

    out = LINK_LIST.sub(link_list, out)
    out = MD_LINK.sub(md_link, out)
    out = URL.sub(bare, out)
    out = INLINE_CODE.sub(lambda m: m.group(1), out)
    out = TABLE_RULE.sub("", out)
    out = RULE.sub("", out)
    out = HEADING.sub("", out)
    out = BULLET.sub("", out)
    out = QUOTE.sub("", out)
    for _ in range(2):  # nested emphasis: ***x*** and **_x_**
        out = EMPHASIS.sub(lambda m: m.group(2), out)
    out = CITATION.sub("", out)
    lines = []
    for raw in out.splitlines():
        line = raw.strip().strip("|").strip()
        if not line:
            continue
        line = re.sub(r"\s*\|\s*", ", ", line)  # a table row: its cells in a row
        line = SPACES.sub(" ", line)
        lines.append(line)
    # each line is a sentence of its own: a heading or a list item has no full stop
    joined = " ".join(l if LINE_END.search(l) else l + "." for l in lines)
    for _ in range(2):
        joined = EMPTY_BRACKETS.sub("", joined)
        joined = SPACE_BEFORE_PUNCT.sub(r"\1", joined)
        joined = REPEATED_PUNCT.sub("", joined)
    joined = SPACES.sub(" ", joined).strip()
    if links:
        joined = TRAILING_LABELS.sub("", joined).strip()
    if joined in {".", ",", ":", ";"}:
        joined = ""
    return joined, links


def _safe(text: str) -> bool:
    """True when ``text`` can be spoken on its own: no link or code block left open."""
    if text.count("```") % 2:
        return False
    if text.count("[") > text.count("]"):
        return False
    if text.count("](") > 0 and text.rfind("](") > text.rfind(")"):
        return False
    return text.count("`") % 2 == 0


class SpeechStream:
    """Streamed answer text in, speakable pieces out: :meth:`feed` returns the finished
    sentences not yet returned ("" while a sentence is still being written), and
    :meth:`finish` the rest. :attr:`links` collects the links of both."""

    def __init__(self) -> None:
        self._buf = ""
        self.links: list[str] = []
        self.spoken = ""

    def _say(self, raw: str) -> str:
        text, links = to_speech(raw)
        for url in links:
            _add(self.links, url)
        if not text:
            return ""
        piece = (" " if self.spoken else "") + text
        self.spoken += piece
        return piece

    def feed(self, delta: str) -> str:
        self._buf += delta or ""
        cut = 0
        for m in SENTENCE_END.finditer(self._buf):
            if _safe(self._buf[:m.end()]):
                cut = m.end()
        if not cut:
            return ""
        raw, self._buf = self._buf[:cut], self._buf[cut:]
        return self._say(raw)

    def finish(self) -> str:
        raw, self._buf = self._buf, ""
        return self._say(raw)


# ------------------------------------------------------------ the final question mark
# Home Assistant keeps the microphone open (and skips its own commands) only when an
# answer ends with "?", so only a confirmation question may end with one.
LAST_SENTENCE = re.compile(r"(?:^|(?<=[.!?…]))\s*([^.!?…]*[?])\s*$")
OFFER = re.compile(
    r"(?i)^\s*(?:is there\s+)?(?:anything else|something else|can i help|how (?:else )?can i"
    r"|would you like (?:to know|more|anything)|do you (?:want|need) (?:anything|more)"
    r"|shall i (?:look|check|search)|want me to (?:look|check|search)|need anything)\b")


def end_without_question(text: str) -> str:
    """``text`` without a question at the end: a trailing offer ("Anything else?") is
    dropped, any other last "?" becomes "."."""
    out = (text or "").rstrip()
    for _ in range(3):
        m = LAST_SENTENCE.search(out)
        if not m:
            return out
        sentence = m.group(1)
        if OFFER.search(sentence) and out[:m.start(1)].strip():
            out = out[:m.start(1)].rstrip()
            continue
        return out[:m.end(1) - 1].rstrip() + "."
    return out


def end_with_question(text: str, question: str) -> str:
    """``text`` ending in the confirmation question ``question``: any question the model
    ended with is dropped first, so a "yes" answers the kept change's own question (built
    from the stored call) and never an offer or a question the model made up."""
    out = (text or "").rstrip()
    q = (question or "").strip()
    if not q:
        return out
    if not q.endswith("?"):
        q = q.rstrip(".!") + "?"
    for _ in range(3):
        m = LAST_SENTENCE.search(out)
        if not m:
            break
        out = out[:m.start(1)].rstrip()
    return (out + " " + q).strip()
