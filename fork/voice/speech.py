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


# The same with any short lead-in that names links, sources or transcripts and starts a
# sentence ("Links to the conversations: [Q4 Budget Cut](u) and [Office move](v).")
LEAD_IN_LIST = re.compile(
    r"(?im)(?:^|(?<=[.!?]))[ \t]*[^.!?:\n\[\]()]{0,60}?"
    r"\b(?:links?|sources?|transcripts?|references?|recordings?)\b[^.!?:\n\[\]()]{0,40}?:\s*"
    r"((?:(?:\[[^\]\n]*\]\([^)\s]+\)|<?https?://[^\s<>()\[\]]+>?)"
    r"(?:\s*(?:,|;|and)\s*)?)+)\s*\.?")
# Parentheses left holding only a link's name once its link went: "(Web )", "(source)"
LABEL_WORDS = frozenset({"web", "website", "site", "page", "article", "see", "via", "at",
                         "more", "info", "details", "full", "the", "a", "on", "in"})
PARENS = re.compile(r"\(([^()\n]{0,40})\)")
# A trailing offer or question: Extended OpenAI Conversation keeps the microphone open
# (Home Assistant's continue_conversation) when an answer ends in one
OFFER = re.compile(
    r"(?i)^(?:would you like|do you want|did you want|shall i|should i|want me to|"
    r"let me know|if you(?:'d| would)? (?:like|want|need)|is there anything|"
    r"anything else|can i help|how can i help|feel free|just let me know|"
    r"i can also|i could also|need anything)")
CLARIFY = re.compile(r"(?i)^(?:which one|which|did you mean|do you mean)\b")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _label_only(m: re.Match) -> str:
    inner = m.group(1)
    words = [w for w in re.findall(r"[\w']+", inner.lower())]
    if all(w in LABEL_WORDS or w in LINK_WORDS for w in words):
        return ""
    return m.group(0)


def strip_offers(text: str) -> str:
    """``text`` without a trailing offer ("Would you like…", "Let me know if…") or a
    trailing question, unless the question asks which item was meant; never empty."""
    sentences = [p for p in SENTENCE_SPLIT.split((text or "").strip()) if p]
    while len(sentences) > 1:
        last = sentences[-1].strip()
        if OFFER.match(last) or (last.endswith("?") and not CLARIFY.match(last)):
            sentences.pop()
            continue
        break
    return " ".join(sentences)


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

    out = LEAD_IN_LIST.sub(link_list, out)
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
        if links:
            joined = PARENS.sub(_label_only, joined)
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
    :meth:`finish` the rest, without a trailing offer or question there
    (:func:`strip_offers`; a model's last sentence has nothing after it, so it is still
    unfinished when the stream ends). :attr:`links` collects the links of both."""

    def __init__(self) -> None:
        self._buf = ""
        self.links: list[str] = []
        self.spoken = ""

    def _say(self, raw: str, *, last: bool = False) -> str:
        text, links = to_speech(raw)
        for url in links:
            _add(self.links, url)
        if last and text:
            if self.spoken:  # the rest follows spoken sentences: it may go entirely
                kept = strip_offers("x. " + text)
                text = kept[3:] if kept.startswith("x. ") else ""
            else:
                text = strip_offers(text)
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
        return self._say(raw, last=True)
