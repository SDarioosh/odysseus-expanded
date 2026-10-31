"""Fork: the brain's check for invented actions (docs/fork/brain.md, "Invented actions").

Upstream's agent holds every answer back until the end (``with_completion_gate``,
``src/agent_runtime/completion.py``) to drop "I've created/sent ..." claims no tool call
backs. The brain streams its answer as it is written, so it can't take a claim back.
Instead, once the answer is complete, :func:`correction` reads it and, when it claims a
change this turn didn't make, returns ONE short note the brain sends after the answer
and saves with it ("Note: nothing was changed in this turn: no calendar event was added
or changed."). No model call, no buffering: pure CPU, linear in the answer's length,
about 1 ms on a 20,000-character answer (at most :data:`MAX_CANDIDATES` claim words are
looked at closely, the first and the last ones).

What counts as a claim (each statement on its own, quotes and code blanked by upstream's
``_unquoted_statements``):

- first person, past or perfect tense: "I've added ...", "I just sent ...", "We saved
  ...", "I've gone ahead and created ...", and verbs joined to one ("..., and sent ...");
- a pronoun subject in the perfect passive: "It's been added to your calendar", "That has
  been saved", "It's now on your calendar"; or a store's noun: "The note has been saved";
- a statement that starts with the verb: "Added to your calendar: ...", "Saved.";
- a bare "Done." / "All set." (upstream's ``_BARE_SUCCESS``, plus "You're all set.") that
  opens the answer; later in the answer only when this turn tried a write ("Here's how
  it works: .... Done." ends an explanation).

Not claims: questions and offers ("Shall I add it?", "I can create ...", "I'll add it
once you approve"), conditional, modal or attributed prose before the verb (upstream's
``_NON_REPORT_SCOPE``: if, when, would, said ...), negations ("I didn't send", "No event
was created"), other turns ("earlier", "yesterday", "already", "last week"), other people
("you created", "Maria sent"), reads ("I found", "I checked"), text shown in the chat
("Here's a draft I wrote:", "... below"), and edits of the chat's own text ("I've added a
few details to the paragraph": an object that names no store and isn't "it"/"that").

A claim names a store when a store's noun is its object or destination ("to your
calendar", "the reminder"), not when the noun only follows of/about/from/for/with ("a
summary of the meeting"); "sent"/"replied"/"forwarded" mean email and
"scheduled"/"booked"/"invited" the calendar when no noun says otherwise. A claim is backed
by a write of this turn that succeeded on a tool of one of its stores (:data:`DOMAIN_TOOLS`;
Athena's action items: an ``mcp__…__update_action_item``); a claim that names no store by
any successful write.
"""
from __future__ import annotations

import functools
import re
from typing import Iterable

# store -> the write tools that change it
DOMAIN_TOOLS: dict[str, frozenset[str]] = {
    "calendar": frozenset({"manage_calendar"}),
    "notes": frozenset({"manage_notes", "manage_tasks"}),
    "email": frozenset({"draft_email", "draft_email_reply", "send_email", "reply_to_email"}),
    "contacts": frozenset({"manage_contact"}),
    "memory": frozenset({"manage_memory"}),
    "documents": frozenset({"create_document", "manage_documents"}),
    "action_items": frozenset(),  # mcp__<server>__update_action_item (tool_domain)
}
# store -> what the note says wasn't done; in this order
NOT_DONE: dict[str, str] = {
    "calendar": "no calendar event was added or changed",
    "notes": "no note, to-do or reminder was saved",
    "email": "no email was sent or drafted",
    "contacts": "no contact was saved",
    "memory": "nothing was saved to memory",
    "documents": "no document was created or changed",
    "action_items": "no action item was updated",
}
GENERIC = ""  # a claim that names no store
LATE_DONE = "done"  # a bare "Done." after other text: a claim only next to a write
# claim words looked at, at most (the first and the last half): keeps the check under a
# few milliseconds on any answer; an answer rarely has more than a few
MAX_CANDIDATES = 80
LONG_LINE = 1500  # a longer line is cut into sentences around its claim words
SENTENCE = 400  # how far back and ahead such a sentence is looked for

# Patterns are matched on lowercased text: re.IGNORECASE makes them several times slower.

# verbs that claim a change whatever their object (with a pronoun or a store's noun)
_STRONG = (r"added|created|saved|scheduled|rescheduled|sent|e-?mailed|deleted|booked|"
           r"stored|filed|forwarded|replied|archived|cancell?ed|marked|"
           r"set\s+(?:up|a|an|the|it|that|this|your)|put(?!\s+together)|"
           r"made\s+(?:a|an)\s+(?:note|entry|event|reminder|to-?do|list)")
# verbs that claim a change only with a store's noun as their object
_WEAK = r"updated|changed|edited|modified|moved|renamed|removed|logged|invited|drafted"
_VERB = rf"(?:{_STRONG}|{_WEAK})\b"
_ADV = (r"(?:\s+(?:just|now|also|successfully|quickly|then|gone\s+ahead\s+and|"
        r"went\s+ahead\s+and))*")
_FIRST = re.compile(rf"(?<![\w'’-])(?:i|we)(?:\s*['’]ve|\s+have)?{_ADV}\s+(?P<verb>{_VERB})")
_JOINED = re.compile(rf"(?:,\s*(?:and\s+)?|\s+and\s+)(?:also\s+|then\s+)?(?P<verb>{_VERB})")
_PASSIVE_VERB = (r"added|created|saved|scheduled|rescheduled|sent|deleted|removed|booked|"
                 r"stored|filed|updated|changed|moved|marked|cancell?ed|forwarded|drafted")
_PASSIVE = re.compile(
    rf"(?<![\w'’-])(?:it|that|this|they|everything|all\s+of\s+(?:it|them))"
    rf"(?:\s*['’]s|\s+has|\s+have|\s*['’]ve)\s+(?:now\s+)?been\s+(?:successfully\s+)?"
    rf"(?P<verb>{_PASSIVE_VERB})\b"
    r"|(?<![\w'’-])(?:it|that|this|they)(?:\s*['’]s|\s+is|\s+are|\s*['’]re)\s+now\s+"
    r"(?P<where>(?:on|in)\s+your\s+(?:calendar|notes|to-?do\s+list|list|contacts|memory|drafts))\b")
_NOUN_PASSIVE = re.compile(
    r"(?<![\w'’-])(?:the|your)\s+(?:[\w-]+\s+){0,2}?(?P<noun>event|appointment|note|reminder|"
    r"to-?do|task|checklist|contact|document|action\s+item|draft)s?\s+"
    r"(?:has|have)\s+(?:now\s+)?been\s+(?:successfully\s+)?"
    r"(?P<verb>added|created|saved|stored|filed|deleted|removed|drafted)\b")
_LEADING = re.compile(
    r"(?P<verb>added|saved|created|scheduled|rescheduled|booked|sent|deleted|removed|"
    r"updated|moved|marked|cancell?ed|stored|filed|forwarded|archived)\b(?!\s+by\b)")
_FORK_BARE = re.compile(
    r"\s*(?:you(?:['’]re|\s+are)\s+all\s+set|(?:that|it)(?:['’]s|\s+is)\s+(?:all\s+)?done|"
    r"all\s+sorted|sorted)\s*[.!]*\s*")

# an answer that opens with a bare "Done." (after list or heading marks)
_DONE_FIRST = re.compile(
    r"[\s\-–—*•>#✅✔☑️]*(?:done|all\s+done|all\s+set|completed|success|fixed|sorted|"
    r"all\s+sorted|you(?:['’]re|\s+are)\s+all\s+set|(?:that|it)(?:['’]s|\s+is)\s+(?:all\s+)?done)"
    r"\s*[.!]*(?:\s|$)")

# a cheap first look: a statement without any of these can't hold a claim
_ANY = re.compile(
    r"\b(?:added|created|saved|scheduled|rescheduled|sent|e-?mailed|deleted|booked|stored|"
    r"filed|forwarded|replied|archived|cancell?ed|marked|set|put|made|updated|changed|edited|"
    r"modified|moved|renamed|removed|logged|invited|drafted|done|sorted|fixed|completed|"
    r"success|now)\b")
_SENTENCE_END = re.compile(r"[.!?;](?=\s)")
_LEAD_MARKS = re.compile(r"^[\s\-–—*•>#✅✔☑️\d.)\]]+")
# blanked before anything else: code blocks and double quotes (as upstream's
# _unquoted_statements does, which then still runs on what is left), and curly quotes
_QUOTED = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|\"[^\"\n]*\"|“[^”\n]*”|«[^»\n]*»|„[^“”\n]*[“”]")
_NEG = re.compile(r"\b(?:not|never|no|nothing|none|neither|nor|without|unable|cannot|"
                  r"failed|instead\s+of)\b|\w+n['’]t\b")
_POLITE = re.compile(r"^\s*(?:no\s+(?:problem|worries)|not\s+a\s+problem)\b[,.!—–-]*")
_SCOPE_BEFORE = re.compile(r"\b(?:once|after|as\s+soon\s+as|before|let\s+me|going\s+to|"
                           r"want\s+me|like\s+me|shall|ready\s+to)\b")
_SCOPE_AFTER = re.compile(r"\s+(?:if|when|whenever|unless|until|once)\b")
_PAST = re.compile(
    r"\b(?:earlier|previously|yesterday|ago|already|last\s+(?:week|month|year|night|time|"
    r"turn|chat)|the\s+other\s+day|this\s+morning|before\s+now|in\s+(?:a|an|our|the|my)\s+"
    r"(?:previous|earlier|last|past|prior)\s+(?:chat|conversation|message|reply|answer|turn))\b")
_ATTRIBUTED = re.compile(r"\b(?:according\s+to|mentioned|told|says|said|reported|recorded)\b")
_SHOWN = re.compile(r"\b(?:below|above|here(?:['’]s)?)\b|:\s*$")
_NOTHING_AFTER = re.compile(r"\s*(?:nothing|no\b|none\b|by\b)")
# a statement about another turn or person, or about text shown in the chat
_NOT_THIS_TURN = re.compile(f"{_PAST.pattern}|{_ATTRIBUTED.pattern}|{_SHOWN.pattern}")
_NOT_AFTER = re.compile(f"{_SCOPE_AFTER.pattern}|{_NOTHING_AFTER.pattern}")
_CLAUSE_END = re.compile(r"[;:(]|\s(?:but|because|which|while|since|so\s+that)\s")
_PRONOUN_OBJECT = re.compile(
    r"\s*(?:it|that|this|them|these|those|everything|all\s+of\s+(?:it|them)|the\s+changes?)\b")

# store nouns, one named group per store (action items before the calendar's "items")
_NOUNS = re.compile(
    r"\b(?:(?P<action_items>action[\s-]+items?)"
    r"|(?P<calendar>calendar|events?|meetings?|appointments?|invites?|invitations?|schedule|agenda)"
    r"|(?P<notes>notes?|to-?dos?|tasks?|checklists?|reminders?|(?:your|the|my)\s+(?:[\w-]+\s+)?list)"
    r"|(?P<email>e-?mails?|mail|inbox|drafts|draft\s+folder|as\s+a\s+draft)"
    r"|(?P<contacts>contacts?|address\s+book|phone\s+book)"
    r"|(?P<memory>memory|memories)"
    r"|(?P<documents>documents?|docs?))\b")
# a noun right after one of these is what the object is about, not where it went
_ABOUT = re.compile(
    r"\b(?:of|about|from|for|with|during|after|before|regarding)\s+"
    r"(?:(?:the|your|my|this|that|a|an|today['’]s|tomorrow['’]s|[\w-]+['’]s)\s+)?"
    r"(?:[\w-]+\s+)?$")
_VERB_STORE = (
    (re.compile(r"^(?:sent|e-?mailed|forwarded|replied)"), "email"),
    (re.compile(r"^(?:scheduled|rescheduled|booked|invited)"), "calendar"),
)
_WEAK_RE = re.compile(rf"^(?:{_WEAK})$")
_DRAFTED_EMAIL = re.compile(r"\s*(?:a|an|the|your)\s+(?:[\w-]+\s+)?(?:reply|response|e-?mail|message)\b")


@functools.lru_cache(maxsize=1)
def _upstream() -> tuple:
    """Upstream's gate patterns and statement splitter, looked up once
    (``fork/upstream.py``)."""
    from fork.upstream import completion_claim_patterns, unquoted_statements

    pats = completion_claim_patterns()
    # upstream's scope words (lowercase literals), the fork's negations and offers, in one
    not_before = re.compile(rf"(?:{pats['non_report'].pattern})|{_NEG.pattern}|"
                            rf"{_SCOPE_BEFORE.pattern}")
    return pats["bare_success"], not_before, unquoted_statements


def tool_domain(name: str) -> str | None:
    """The store a write tool changes, or None (a write of no listed store)."""
    for domain, tools in DOMAIN_TOOLS.items():
        if name in tools:
            return domain
    if name.startswith("mcp__") and name.endswith("__update_action_item"):
        return "action_items"
    return None


def _blank(match: re.Match) -> str:
    return re.sub(r"[^\n]", " ", match.group())


def _stores(verb: str, after: str) -> set[str] | None:
    """The stores a claim names: from the nouns in ``after`` (the verb's clause after it)
    that are its object or destination, else from the verb. ``{GENERIC}`` when it names
    none and its object is "it"/"that" (or it has none); None when it isn't a claim of a
    change: a weak verb without a store's noun, or an object that names no store ("a few
    details to the paragraph", "a summary of the meeting"). Email nouns don't count for
    weak verbs: "I've edited your draft" is the chat's own text."""
    end = _CLAUSE_END.search(after)
    clause = after[:end.start()] if end else after
    word = verb.split()[0] if verb.strip() else ""
    weak = bool(_WEAK_RE.match(word))
    found: set[str] = set()
    for m in _NOUNS.finditer(clause):
        domain = m.lastgroup
        if domain in found or (weak and domain == "email"):
            continue
        if not _ABOUT.search(clause[:m.start()]):
            found.add(domain)
    if found:
        return found
    if word == "drafted" and _DRAFTED_EMAIL.match(clause):
        return {"email"}
    for pattern, domain in _VERB_STORE:
        if pattern.match(word):
            return {domain}
    if weak:
        return None
    if not clause.strip(" \t.!,") or _PRONOUN_OBJECT.match(clause):
        return {GENERIC}
    return None


def _ok_before(statement: str, start: int, not_before: re.Pattern) -> bool:
    return not not_before.search(_POLITE.sub("", statement[:start]))


def _statement_claims(s: str, bare: re.Pattern, not_before: re.Pattern) -> list[set[str]]:
    s = _LEAD_MARKS.sub("", s.replace("*", "")).strip()
    if not s or s.endswith("?") or not _ANY.search(s):
        return []
    if bare.fullmatch(s) or _FORK_BARE.fullmatch(s):
        return [{LATE_DONE}]
    if _NOT_THIS_TURN.search(s):
        return []
    found: list[tuple[int, int, str, str]] = []  # start, end, verb, where
    first = list(_FIRST.finditer(s))
    for m in first:
        found.append((m.start(), m.end(), m.group("verb"), ""))
    if first:
        for m in _JOINED.finditer(s, first[0].end()):
            found.append((m.start(), m.end(), m.group("verb"), ""))
    for m in _PASSIVE.finditer(s):
        found.append((m.start(), m.end(), m.group("verb") or "", m.group("where") or ""))
    for m in _NOUN_PASSIVE.finditer(s):
        found.append((m.start(), m.end(), m.group("verb"), m.group("noun")))
    lead = _LEADING.match(s)
    if lead:
        found.append((0, lead.end(), lead.group("verb"), ""))
    if not found:
        return []
    found.sort()
    claims = []
    for i, (start, end, verb, where) in enumerate(found):
        if not _ok_before(s, start, not_before):
            continue
        nxt = found[i + 1][0] if i + 1 < len(found) else len(s)
        after = s[end:max(end, nxt)]
        if _NOT_AFTER.match(after):
            continue
        stores = _stores(verb, f"{where} {after}" if where else after)
        if stores is not None:
            claims.append(stores)
    return claims


def _claim_spans(text: str) -> list[str]:
    """The parts of ``text`` (quotes and code blanked) that hold a claim word, in order,
    each once: the word's line, or in a line over :data:`LONG_LINE` characters the
    sentence around it. Upstream's statement splitter runs on these parts only (its
    quotes never span a line). At most :data:`MAX_CANDIDATES` words: the first and the
    last half."""
    hits = [m.start() for m in _ANY.finditer(text)]
    if len(hits) > MAX_CANDIDATES:  # a long list of claims: its first and last ones
        half = MAX_CANDIDATES // 2
        hits = hits[:half] + hits[-half:]
    spans: list[str] = []
    span_end = -1
    for i in hits:
        if i < span_end:
            continue
        start = text.rfind("\n", 0, i) + 1
        span_end = text.find("\n", i)
        if span_end < 0:
            span_end = len(text)
        if span_end - start > LONG_LINE:
            back = text.rfind(". ", max(start, i - SENTENCE), i)
            start = back + 2 if back >= 0 else max(start, i - SENTENCE)
            ahead = _SENTENCE_END.search(text, i, min(span_end, i + SENTENCE))
            span_end = ahead.end() if ahead else min(span_end, i + SENTENCE)
        spans.append(text[start:span_end])
    return spans


def claims(text: str) -> list[set[str]]:
    """The claims of a change in ``text``: per claim, the stores it names (``{GENERIC}``:
    none; ``{LATE_DONE}``: a bare "Done." after other text)."""
    low = _QUOTED.sub(_blank, (text or "").lower())
    spans = _claim_spans(low)
    if not spans:
        return []
    bare, not_before, unquoted = _upstream()
    found = [scoped for span in spans for _statement, scoped in unquoted(span)
             if _ANY.search(scoped)]
    out: list[set[str]] = []
    for scoped in found:
        out.extend(_statement_claims(scoped, bare, not_before))
    if out and out[0] == {LATE_DONE} and _DONE_FIRST.match(low):
        out[0] = {GENERIC}  # the answer opens with it: "Done." / "All set! ..."
    return out


def correction(text: str, writes: Iterable[tuple[str, bool]]) -> str:
    """The note to add after ``text``, or "": ``writes`` are this turn's write calls as
    ``(tool name, succeeded)``, in order."""
    found = claims(text)
    if not found:
        return ""
    writes = list(writes)
    done = [name for name, ok in writes if ok]
    done_stores = {tool_domain(name) for name in done}
    missing: set[str] = set()
    for stores in found:
        if stores == {LATE_DONE}:
            # "... like this: function f() {}. Done." ends an explanation; after a write
            # that failed or was refused it claims that write
            if writes and not done:
                missing.add(GENERIC)
        elif stores == {GENERIC}:
            if not done:
                missing.add(GENERIC)
        elif not (stores - {GENERIC}) & done_stores:
            missing |= stores - {GENERIC}
    if not missing:
        return ""
    phrases = [NOT_DONE[d] for d in NOT_DONE if d in missing]
    listed = ", and ".join(phrases) if len(phrases) < 3 else \
        ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    if not done:
        body = "nothing was changed in this turn" + (f": {listed}" if listed else "")
    else:
        body = f"{listed} in this turn"
    return f"\n\nNote: {body}."
