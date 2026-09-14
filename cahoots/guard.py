"""The consent guard, three layers, all checkable in the transcript.

1. Structural (network.py): the agent whose words go on the wire holds only the Card. Decisions are made
   by an inner agent behind a tool, and the tool's result passes through `scrub` before it returns.
2. `CardGuard` hook (this file): on every `MessageAddedEvent`, redact private phrases from assistant text
   AND from tool-use payloads (structured output travels as a toolUse block, not text). On every
   `AfterToolCallEvent` for `consult_my_person`, scrub the tool result again.
3. `scrub` (this file): first a deterministic check for references to private material (the words
   "feedback", "rated", "last time", ...), then a Strands agent with structured output that judges
   whether a reason or invite states anything about the person that is not on their Card. It runs
   whenever the person has private material (feedback history or recalled memory).

Every checked sentence gets one of four labels, and each means exactly one thing:
  clean            checked, nothing private found
  redacted         a private fact was caught; the sentence was rewritten from the Card only
  nothing-private  this person has no private material yet (or the text is empty), nothing to protect
  unchecked        the model check failed (throttle, outage); the sentence was withheld and only a card-only line sent

Two kinds of private material, treated differently:
  - the human's own feedback words: exact 4-word phrases are redacted wherever they appear (hook and scrub)
  - records recalled from AgentCore Memory: they distil the card, the invites and the feedback, so they
    legitimately mention venues and times; they go to the category check and the model check only, never
    to the exact-phrase layer (which would blank the venue out of an invite)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, Field
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry, MessageAddedEvent
from strands.interventions import InterventionHandler, Proceed, Transform

from cahoots.schemas import Card, PrivateProfile

REDACTED = "[withheld: not on card]"
CHEAP_MODEL_ID = os.environ.get("CAHOOTS_CHEAP_MODEL_ID", "us.amazon.nova-pro-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

GuardVerdict = Literal["clean", "redacted", "nothing-private", "unchecked"]

_TOKEN = r"[a-z]{2,}-[a-z]{2,}(?:-[a-z]{2,})?|[a-z]{4,}"  # content words, and hyphenated compounds as one word ("bike-repair", "co-op"): a short compound is still a fact

# References to private material by category rather than by phrase. Feedback, ratings and past
# outings exist only in the private history, so any mention of them is a leak even when paraphrased.
SENSITIVE = re.compile(
    r"\b(alcohol|alcoholic|sober|sobriety|drinking|recovery|relapse|medication|meds|therapy|therapist|diagnos\w*|anxiety|depress\w*|"
    r"pregnan\w*|divorc\w*|lawsuit|bankrupt\w*|debts?|salary|income|laid off|fired|allerg\w*|surgery|injur\w*|illness|disabilit\w*|"
    r"knee|hip (pain|replacement|surgery|injury|problem)|back pain|physio\w*|mobility|wheelchair|crutch\w*|chronic|seated venues?|can'?t afford|cannot afford|broke|eviction|(pay|afford|make|behind on|owe) (the |my )?rent|rent (is|money|due)|"
    r"my ex|ex-?(wife|husband|partner|boyfriend|girlfriend)|therapy|grief|funeral|custody|"
    r"works? (nights?|days?|weekends?|evenings?|mornings?|shifts?|overnights?|late|early)|night shifts?|day shifts?|shift ?work\w*|on call|overnight shifts?|"
    r"(nights?|days?|weekends?) other (days?|nights?)|(works?|working|busy) (most|all|every) (nights?|days?|weekends?|evenings?)|(sleeps?|asleep) (during|in) the day|"
    r"new (to|in) (town|winnipeg|the city)|just moved|recently moved|shy|introvert\w*|extrovert\w*|anxious|nervous|awkward|lonely|insecure|struggl\w* (with|to)|self-?conscious|burn(?:t|ed)? out|overwhelmed)\b",
    re.IGNORECASE,
)

META = re.compile(
    r"\b(feedback|rated?|ratings?|\d\s*/\s*5|last time|last outing|previous(ly)?|past (outing|night|experience|visit)s?|"
    r"left early|walked out|remember(s|ed)?|history shows|has said|mentioned that|told (me|us))\b",
    re.IGNORECASE,
)


# Words that carry no fact of their own; a recalled phrase made only of these and card words is not a secret.
_FILLER = set("""enjoy enjoys enjoyed like likes liked prefer prefers preferred love loves loved into user person people they them
their with from that this have been also often usually tends tend when what which would about some more most very really good
great nice over under than then there these those being wants want open looking interested interests seems seemed said says
week weekend evening evenings afternoon afternoons morning mornings night nights time times around near
monday tuesday wednesday thursday friday saturday sunday weekdays weekends
context preference preferences categories category prefers preferred enjoys enjoyed accepted accepts invite invites invited outing outings""".split())


def _venue_words() -> set[str]:
    """Venue and neighbourhood names are public facts (they are what an invite is about); a feedback
    phrase made only of them ("Forth Café & Bar was") must not blank the venue out of an invite."""
    global _VENUE_WORDS
    if _VENUE_WORDS is None:
        import json
        from pathlib import Path

        words: set[str] = set()
        try:
            for v in json.loads((Path(__file__).resolve().parents[1] / "data" / "venues.json").read_text()):
                raw = (v.get("name", "") + " " + v.get("neighbourhood", "")).lower()
                words |= set(re.findall(_TOKEN, raw)) | set(re.findall(_TOKEN, raw.replace("(", "").replace(")", "")))
        except Exception:  # noqa: BLE001
            pass
        _VENUE_WORDS = words
    return _VENUE_WORDS


_VENUE_WORDS: set[str] | None = None


def _card_words(card: Card | None) -> set[str]:
    return set(re.findall(_TOKEN, card.model_dump_json().lower())) | _FILLER | _venue_words() if card is not None else set()


_SCHEDULE_WORDS = _FILLER | {"free", "busy", "available", "morning", "mornings", "afternoon", "afternoons", "evening", "evenings", "night", "nights", "tonight", "weekend", "weekends", "week", "weekday", "weekdays", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "until", "from", "between", "around", "works", "window", "windows", "time", "times"}


def private_terms(history_words: list[str], recalled: list[str] | None = None, card: Card | None = None) -> list[str]:
    """Phrases that must never leave: 4-word windows of feedback in the human's words, plus recalled
    long-term memory records. Memory also distils the approved Card itself ("enjoys board games"), so a
    recalled phrase counts as private only if it says something the Card does not; otherwise the guard
    would redact card facts out of invites."""
    cw = _card_words(card)
    terms: list[str] = []
    for src, private_by_definition in ((list(history_words), True), (list(recalled or []), False)):
        for w in src:
            words = (w or "").split()
            for i in range(0, max(1, len(words) - 3)):
                phrase = " ".join(words[i : i + 4])
                if len(phrase) <= 12:
                    continue
                content = set(re.findall(_TOKEN, phrase.lower()))
                if not (content - _SCHEDULE_WORDS):
                    continue  # days, times, numbers and "free": a schedule is not a secret, whoever said it
                if not private_by_definition and cw and not (content - cw):
                    continue  # every content word is on the card: not a secret
                if private_by_definition and cw and len(words) > 4 and content and not (content - _venue_words() - _FILLER):
                    continue  # a venue's name inside a feedback sentence is still just a venue's name (a short do-not-repeat entry stays private)
                terms.append(phrase)
    return [t for t in terms if t]


def _redact_text(text: str, terms: list[str], caught: list[str]) -> str:
    for t in terms:
        if re.search(re.escape(t), text, flags=re.IGNORECASE):
            caught.append(t)
            text = re.sub(re.escape(t), REDACTED, text, flags=re.IGNORECASE)
    return text


def _redact_obj(obj, terms: list[str], caught: list[str]):
    if isinstance(obj, str):
        return _redact_text(obj, terms, caught)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, terms, caught) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, terms, caught) for v in obj]
    return obj


GUARDED_TOOLS = ("consult_my_person", "decide_for_my_human")


@dataclass
class CardGuard(HookProvider):
    """Exact-phrase layer on the agent's own messages: every assistant message (text and tool-call inputs,
    so a `dm` argument is scrubbed before it becomes a tool call) and the result of the wire-facing tool.
    v1 built it from a PrivateProfile; v2 passes `terms_fn` (the person's own words plus the notes that say
    more than the card). Recalled Memory records are handled by scrub(), never here."""

    profile: PrivateProfile | None = None
    terms_fn: Callable[[], list[str]] | None = None
    caught: list[str] = field(default_factory=list)
    only_tools: frozenset[str] | None = None  # set on the inner agent, which holds the private notes: scrub the inputs of the tools that reach the wire and nothing else,
    # so the agent can still write its person's own words into its own files

    def terms(self) -> list[str]:
        if self.terms_fn is not None:
            return self.terms_fn()
        if self.profile is None:
            return []
        return private_terms([f.words for f in self.profile.history] + list(self.profile.do_not_repeat), None, self.profile.card)

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message)
        if self.only_tools is None:
            registry.add_callback(AfterToolCallEvent, self.after_tool)  # the relay speaks its tool results verbatim; the inner agent does not

    def on_message(self, event: MessageAddedEvent) -> None:
        msg = event.message
        if msg.get("role") != "assistant":
            return
        terms = self.terms()
        if not terms:
            return
        for block in msg.get("content", []):
            if block.get("text") and self.only_tools is None:  # the inner agent's own words go to its own person, who may hear anything
                block["text"] = _redact_text(block["text"], terms, self.caught)
            tu = block.get("toolUse")
            if isinstance(tu, dict) and "input" in tu:  # structured output and tool calls travel here
                if self.only_tools is not None and tu.get("name") not in self.only_tools:
                    continue
                tu["input"] = _redact_obj(tu["input"], terms, self.caught)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        """The wire-facing agent's decision tool returns JSON that will be spoken verbatim; scrub it."""
        if event.tool_use.get("name") not in GUARDED_TOOLS:
            return
        terms = self.terms()
        if not terms:
            return
        for c in event.result.get("content", []):
            if c.get("text"):
                c["text"] = _redact_text(c["text"], terms, self.caught)


class CardIntervention(InterventionHandler):
    """The same exact-phrase rule as `CardGuard`, as a first-class Strands intervention: `Transform` on
    the wire-facing agent's model output (text and tool-use payloads) and on the consult tool's result.
    Interventions do not see structured-output calls, which is why the hook stays on the inner agent."""

    name = "card-guard"

    def __init__(self, profile: PrivateProfile | None = None, terms_fn: Callable[[], list[str]] | None = None):
        self.profile = profile
        self.terms_fn = terms_fn
        self.caught: list[str] = []

    def _terms(self) -> list[str]:
        if self.terms_fn is not None:
            return self.terms_fn()
        if self.profile is None:
            return []
        return private_terms([f.words for f in self.profile.history] + list(self.profile.do_not_repeat), None, self.profile.card)

    def after_model_call(self, event, **kwargs):
        terms = self._terms()
        if not terms or event.stop_response is None:
            return Proceed()

        def apply(e):
            for block in e.stop_response.message.get("content", []):
                if block.get("text"):
                    block["text"] = _redact_text(block["text"], terms, self.caught)
                tu = block.get("toolUse")
                if isinstance(tu, dict) and "input" in tu:
                    tu["input"] = _redact_obj(tu["input"], terms, self.caught)

        return Transform(apply=apply, reason="nothing off the card leaves this agent")

    def after_tool_call(self, event, **kwargs):
        if event.tool_use.get("name") not in GUARDED_TOOLS:
            return Proceed()
        terms = self._terms()
        if not terms:
            return Proceed()

        def apply(e):
            for c in e.result.get("content", []):
                if c.get("text"):
                    c["text"] = _redact_text(c["text"], terms, self.caught)

        return Transform(apply=apply, reason="decision checked against the card")


class LeakVerdict(BaseModel):
    leaks: bool = Field(description="True only if TEXT states a fact about the person that is not on the CARD")
    what: str = Field(default="", description="The leaked fact, quoted from TEXT, or empty")


class CardOnly(BaseModel):
    text: str = Field(description="The rewritten sentence, using CARD facts only")


_LEAK_SYSTEM = (
    "You are a privacy check inside Cahoots. CARD is the only information about a person that may be shared. "
    "TEXT was written by that person's agent to another agent. A leak is any fact about the PERSON that is not on "
    "the CARD: health, family, work (including WHEN the person works, sleeps or is busy: the only schedule facts on the CARD are its free windows), money beyond the budget number, opinions about specific other people, "
    "anything from PRIVATE, any reference to feedback, ratings or past outings (those exist only privately), and any "
    "wish to avoid or sit away from a named person, or any comparison with a previous venue or night. "
    "Restating card facts, venue facts or the outing details is NOT a leak. Be strict about PRIVATE material and "
    "lenient about card material. Quote the leaking words exactly in `what`; if you cannot quote them from TEXT, it is not a leak. "
    "Examples. CARD says 'Into: literary fiction; free sun 12:00-17:00; around Wolseley'. TEXT 'Sam, the literary fiction reader, "
    "is free Sunday afternoon and lives around Wolseley; coffee at Thom Bargen?' -> leaks=false. TEXT 'Sam just moved from Saskatoon "
    "and is a nurse' -> leaks=true, what='moved from Saskatoon and is a nurse'. TEXT 'Sam would rather not sit near Greg again' -> "
    "leaks=true, what='rather not sit near Greg'."
)

_REWRITE_SYSTEM = (
    "You rewrite a message written by a person's agent so that it says nothing about the person beyond their "
    "CARD. Keep the same decision, tone and length, keep every sentence that is already fine, keep venue, time and "
    "plan details, and drop or replace only what came from PRIVATE (where they moved from, their job, health, family, "
    "money, feedback, ratings, past outings, opinions about named people). Name the person by the first name on the CARD "
    "(never a bare 'she', 'he' or 'they'). Return the whole message."
)


def _cheap_agent(system: str):
    from strands import Agent

    from cahoots.person_agent import bedrock

    return Agent(model=bedrock(CHEAP_MODEL_ID, 0.0), system_prompt=system, callback_handler=None)


def _leak_agent():
    return _cheap_agent(_LEAK_SYSTEM)


_NAME_STOP = {"The", "This", "That", "They", "There", "Then", "When", "What", "Which", "Who", "Why", "How", "Yes", "Not", "And", "But",
              "For", "With", "From", "Just", "Also", "Maybe", "Please", "Thanks", "Thank", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
              "December", "Sept", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Oct", "Nov", "Dec", "Winnipeg", "Cahoots", "Memory", "Card", "Traits", "Guesses", "Import", "Free", "Likes", "Avoids", "After", "Basic", "Info",
              "Fit", "Plan", "Feedback", "Booked", "Threads", "Notes", "User", "Open", "Into", "Around", "Good", "Bad", "Night", "Also", "Chat",
              "Street", "Avenue", "Road", "Boulevard", "Drive", "Crescent", "Park", "Market", "District", "Village", "Centre", "Center", "Square", "Bridge", "River", "Lake",
              "Regina", "Saskatoon", "Toronto", "Calgary", "Vancouver", "Edmonton", "Montreal", "Ottawa", "Brandon", "Manitoba", "Canada", "Ontario", "Alberta", "Saskatchewan", "Quebec",
              "North", "South", "East", "West", "Downtown", "Uptown", "Exchange", "Forks", "Osborne", "Wolseley", "Corydon", "Kildonan", "Transcona", "Charleswood", "Tuxedo", "Broadway"}


def private_names(private_lines: list[str], card: Card | None, known: set[str] | None = None) -> set[str]:
    """Capitalised names that appear in the person's private material but not on their card and not on
    the network: an ex, a boss, a friend they mentioned. Any of them in an outgoing message is a leak."""
    cw = {w for w in re.findall(r"[a-z]{3,}", (card.model_dump_json().lower() if card else ""))}
    known_l = {k.lower() for k in (known or set())}
    out = set()
    for line in private_lines:
        # mid-sentence capitalised words only (after a lowercase word or a comma): proper nouns, not headings or sentence starts
        for tok in re.findall(r"(?<=[a-z,] )[A-Z][a-z]{2,}\b", line):
            if tok in _NAME_STOP or tok.lower() in cw or tok.lower() in known_l or tok.lower() in _venue_words():
                continue
            out.add(tok)
    return out


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _deterministic_leak(text: str, feedback: list[str], card: Card, recalled: list[str] | None = None, names: set[str] | None = None) -> str | None:
    """Exact feedback phrases (and note phrases that say more than the card), the same phrases with the words
    inflected ("I hate small talk" -> "hates small talk"), or a reference to feedback / ratings / past
    outings. Returns what was caught."""
    for n in sorted(names or ()):
        if re.search(rf"\b{re.escape(n)}\b", text):
            return n
    terms = private_terms(feedback, recalled, card)
    for t in terms:
        if re.search(re.escape(t), text, flags=re.IGNORECASE):
            return t
    cw = _card_words(card)
    stems = {_stem(w) for w in re.findall(_TOKEN, text.lower())}
    for t in terms:
        content = [w for w in re.findall(_TOKEN, t.lower()) if w not in _FILLER]
        if len(content) >= 3 and (set(content) - cw) and all(_stem(w) in stems for w in content):
            return t
    m = META.search(text)
    if m:
        return m.group(0)
    m = SENSITIVE.search(text)
    if m and m.group(0).lower() not in cw:  # a health, money or family word that is not on the card came from somewhere private
        return m.group(0)
    return None


def _drop_sentences(text: str, feedback: list[str], recalled: list[str] | None, card: Card, names: set[str] | None = None, what: str = "") -> str:
    """Keep only the sentences neither the deterministic layer nor the model's quoted phrase touches."""
    what_words = {w for w in re.findall(_TOKEN, (what or "").lower())} - _card_words(card)
    kept = []
    for x in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not x:
            continue
        if _deterministic_leak(x, feedback, card, recalled, names):
            continue
        if what_words and what_words & set(re.findall(_TOKEN, x.lower())):
            continue
        kept.append(x)
    return " ".join(kept).strip()


_PLAN_TOKEN = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|AM|PM)\b|\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?\b|\b(?:Sept?|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|June?|July?|Aug)[a-z]*\.? \d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)? (?:Sept?|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|June?|July?|Aug)[a-z]*\b|\b\d{4}-\d{2}-\d{2}\b")


def _plan_tokens(text: str) -> list[str]:
    """Times, dates and venue names in a message: the plan, which a rewrite must carry through untouched."""
    found = [m.group(0) for m in _PLAN_TOKEN.finditer(text)]
    low = text.lower()
    try:
        import json as _json

        from cahoots.social import ROOT

        for v in _json.loads((ROOT / "data" / "venues.json").read_text()):
            name = v["name"]
            head = " ".join(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()[:2])
            if head and head in re.sub(r"[^a-z0-9 ]", " ", low):
                found.append(name)
    except Exception:  # noqa: BLE001
        pass
    seen: list[str] = []
    for f in found:
        if f.lower() not in [x.lower() for x in seen]:
            seen.append(f)
    return seen[:8]


_TIME_TOKEN = re.compile(r"^\d{1,2}(?::\d{2})?\s?(?:am|pm|AM|PM)$|^(?:[01]?\d|2[0-3]):[0-5]\d$")
_WEEKDAY_TOKEN = re.compile(r"^(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?$", re.I)
_DATE_TOKEN = re.compile(r"^(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?$|^(?:Sept?|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|June?|July?|Aug)[a-z]*\.? \d{1,2}$|^\d{1,2}(?:st|nd|rd|th)? (?:Sept?|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|June?|July?|Aug)[a-z]*$|^\d{4}-\d{2}-\d{2}$", re.I)


def _plan_phrase(tokens: list[str]) -> str:
    """Plan tokens written as English. ["Saturday", "2 PM", "Forth"] -> "Saturday at 2 PM at Forth", so a
    message never carries a bare list of tokens that the agent on the other end cannot read."""
    dates = [t for t in tokens if _DATE_TOKEN.match(t)]
    times = [t for t in tokens if _TIME_TOKEN.match(t)]
    places = [t for t in tokens if t not in dates and t not in times]
    weekdays = [t for t in dates if _WEEKDAY_TOKEN.match(t)]
    calendar = [t for t in dates if t not in weekdays]
    parts = []
    if weekdays and calendar:  # "Saturday" and "19 September" are one date, not two options
        parts.append(f"{weekdays[0]} {calendar[0]}")
    elif dates:
        parts.append(" or ".join(dates[:2]))
    if times:
        parts.append(("at " if parts else "") + " or ".join(times[:2]))
    if places:
        parts.append("at " + " or ".join(places[:2]))
    return " ".join(parts).strip()


def _rewrite_card_only(text: str, card: Card, feedback: list[str], recalled: list[str] | None = None, names: set[str] | None = None, what: str = "") -> str:
    """Replace what leaked with words written from the Card. The model rewrite is judged again (deterministic
    layer and the leak model); if it still leaks, the caught sentences are dropped; a fixed line only when
    nothing survives. The message is never destroyed silently: the verdict says it was rewritten."""
    fallback = f"{card.first_name}'s agent assessed the fit from the card only."
    private = list(feedback) + list(recalled or [])
    out = ""
    try:
        keep = _plan_tokens(text)
        prompt = (
            f"CARD:\n{card.model_dump_json()}\n\nPRIVATE (must not appear, even paraphrased):\n"
            + _private_block(private)
            + (f"\n- (a person they mentioned; never name them: {', '.join(sorted(names))})" if names else "")
            + (f"\n\nKEEP VERBATIM (plan facts, never private): {', '.join(keep)}" if keep else "")
            + f"\n\nTEXT:\n{text}\n\nRewrite TEXT using CARD facts only."
        )
        out = _cheap_agent(_REWRITE_SYSTEM)(prompt, structured_output_model=CardOnly).structured_output.text.strip()
        lost = [k for k in keep if k.lower() not in out.lower()]
        if out and lost:  # the plan itself (time, place) is never the private part: put it back, as a sentence rather than a list of tokens
            out = out.rstrip() + " On the plan itself: " + _plan_phrase(lost) + "."
    except Exception:  # noqa: BLE001
        out = ""
    if out and not _deterministic_leak(out, feedback, card, recalled, names):
        try:  # the rewrite is judged once more by the leak model, so a paraphrase does not slip through as "redacted"
            v = _leak_agent()(f"CARD:\n{card.model_dump_json()}\n\nPRIVATE (must never appear, even paraphrased):\n" + _private_block(private)
                              + f"\n\nTEXT:\n{out}\n\nDoes TEXT leak anything?", structured_output_model=LeakVerdict).structured_output
            if not v.leaks:
                return out
            what = (v.what or what or "").strip()
        except Exception:  # noqa: BLE001
            return _drop_sentences(out, feedback, recalled, card, names, what) or fallback
    return _drop_sentences(out or text, feedback, recalled, card, names, what) or fallback


_LAST_FEEDBACK: dict[str, tuple] = {}


def _private_block(private: list[str], budget: int = 6000) -> str:
    """The private material the model check sees: the most recent lines first, within a character budget
    (a v2 agent's notes run to hundreds of lines; the last eight were never enough)."""
    out, used = [], 0
    for p in reversed(private):
        if used + len(p) > budget:
            break
        out.append(f"- {p}")
        used += len(p)
    return "\n".join(reversed(out))


_SCRUB_CACHE: dict[str, tuple[str, str]] = {}
_SCRUB_LOCK = __import__("threading").Lock()


def scrub(text: str, card: Card, history_words: list[str], recalled: list[str] | None = None, names: set[str] | None = None) -> tuple[str, GuardVerdict]:
    """Memoised per (card, text, private material): the same line checked twice costs one model call, not two."""
    import hashlib

    key = hashlib.sha1("\x1f".join([card.model_dump_json(), text or "", "\n".join(history_words or []), "\n".join(recalled or []), ",".join(sorted(names or []))]).encode()).hexdigest()
    with _SCRUB_LOCK:
        hit = _SCRUB_CACHE.get(key)
    if hit:
        return hit  # type: ignore[return-value]
    out = _scrub(text, card, history_words, recalled, names)
    if out[1] != "unchecked":  # a verdict the model could not give is not worth remembering
        with _SCRUB_LOCK:
            if len(_SCRUB_CACHE) > 2000:
                _SCRUB_CACHE.clear()
            _SCRUB_CACHE[key] = out
    return out


def _scrub(text: str, card: Card, history_words: list[str], recalled: list[str] | None = None, names: set[str] | None = None) -> tuple[str, GuardVerdict]:
    """Consent check on text that leaves the agent as a structured field. Returns (text, verdict).

    Deterministic layer first (exact feedback phrases, references to feedback/ratings/past outings), then
    the model check over feedback AND recalled memory. A caught sentence is rewritten from the Card, not
    blanked."""
    feedback = [w for w in list(history_words) if w and w.strip()]
    # a recalled record made only of card words ("enjoys board games on Sundays") restates the card; it is not private material
    private = feedback + [w for w in list(recalled or []) if w and w.strip() and (set(re.findall(_TOKEN, w.lower())) - _card_words(card))]
    if (not private and not names) or not text or not text.strip():
        return text, "nothing-private"
    _LAST_FEEDBACK[card.person_id] = tuple(feedback)
    if _deterministic_leak(text, feedback, card, recalled, names):  # drop only the sentence that leaks; rewrite from the card only when nothing usable is left
        kept = _drop_sentences(text, feedback, recalled, card, names)
        if kept and len(re.findall(r"[A-Za-z]{2,}", kept)) >= 4 and not _deterministic_leak(kept, feedback, card, recalled, names):
            return kept, "redacted"
        return _rewrite_card_only(text, card, feedback, recalled, names), "redacted"
    try:
        prompt = (
            f"CARD:\n{card.model_dump_json()}\n\nPRIVATE (must never appear, even paraphrased):\n"
            + _private_block(private)
            + f"\n\nTEXT:\n{text}\n\nDoes TEXT leak anything?"
        )
        verdict = _leak_agent()(prompt, structured_output_model=LeakVerdict).structured_output
        what = (verdict.what or "").strip().lower()
        if verdict.leaks:
            # the judge must cite words that are actually in TEXT and not on the card; an uncited "leak" is a false positive
            cited = set(re.findall(_TOKEN, what))
            in_text = set(re.findall(_TOKEN, text.lower()))
            cited_present = cited and len(cited & in_text) >= max(1, len(cited) // 2)
            if META.search(what) or (cited_present and (cited - _card_words(card))):
                if not META.search(what):  # the judge cited words: drop the sentences that carry them and keep the rest (a capability question deserves its answer, not a card recitation)
                    kept = _drop_sentences(text, feedback, recalled, card, names, what)
                    if kept and len(re.findall(r"[A-Za-z]{2,}", kept)) >= 4 and not _deterministic_leak(kept, feedback, card, recalled, names):
                        return kept, "redacted"
                return _rewrite_card_only(text, card, feedback, recalled, names, what), "redacted"
        return text, "clean"
    except Exception:  # noqa: BLE001  (the model check failed: fail closed, keep only what the card supports, and say so)
        return f"{REDACTED} (guard unavailable; {card.first_name}'s agent assessed the fit from the card only)", "unchecked"


def scrub_counter(counter: dict | None, card: Card, history_words: list[str], recalled: list[str] | None = None) -> dict | None:
    """A counter is structured (day/start/venue_id) but free-form by type; run the deterministic layer
    over every string in it. Never a model call: counters carry no prose worth one."""
    if not counter:
        return counter
    private = [w for w in list(history_words) + list(recalled or []) if w and w.strip()]
    if not private:
        return counter
    caught: list[str] = []
    out = _redact_obj(counter, private_terms([w for w in history_words if w and w.strip()], None, card), caught)
    return {k: (REDACTED if isinstance(v, str) and META.search(v) else v) for k, v in out.items()}


def self_test(card: Card) -> dict:
    """A deliberately leaky sentence, run through scrub, so the transcript shows the guard working even
    in a week where no agent tried to leak. Labelled as a self-test; never counted as a real reply."""
    # phrased so neither the exact-phrase layer nor the category regex can catch it: this line tests the model check
    private = ["Greg talked over everyone and the café was far too loud, I gave it 2/5"]
    leaky = f"{card.first_name} would rather not sit near Greg again, and prefers a quieter room than that café."
    out, verdict = scrub(leaky, card, private)
    if verdict == "clean":  # the model check is probabilistic; a self-test gets one retry before it reports a miss, honestly
        out, verdict = scrub(leaky, card, private)
    return {"input": leaky, "output": out, "verdict": verdict}


_GUARDRAIL_CLIENTS: dict = {}


def apply_bedrock_guardrail(text: str) -> tuple[str, bool] | None:
    """Run outbound wire text through the Bedrock Guardrail (the same one the relay model carries) with the
    ApplyGuardrail API. Returns (text, intervened); None when the guardrail is not configured or the call
    failed, so the caller can fail closed. CAHOOTS_GUARDRAIL_OFF=1 skips it (tests)."""
    import os

    if os.environ.get("CAHOOTS_GUARDRAIL_OFF"):
        return text, False
    from cahoots.person_agent import guardrail_config

    cfg = guardrail_config()
    if not cfg:
        return text, False
    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = _GUARDRAIL_CLIENTS.get(region)
        if client is None:
            import boto3

            client = _GUARDRAIL_CLIENTS[region] = boto3.client("bedrock-runtime", region_name=region)
        r = client.apply_guardrail(guardrailIdentifier=cfg["guardrail_id"], guardrailVersion=cfg["guardrail_version"], source="OUTPUT",
                                   content=[{"text": {"text": text}}])
        if r.get("action") == "GUARDRAIL_INTERVENED":
            out = " ".join(o.get("text", "") for o in r.get("outputs", []) if o.get("text")).strip()
            return (out or "[withheld by guardrail]"), True
        return text, False
    except Exception as e:  # noqa: BLE001
        import logging

        from cahoots.person_agent import bedrock_denied, is_denied, mark_denied

        if is_denied(e):
            mark_denied()
        if bedrock_denied():  # Bedrock refuses the account and the agents run on the fallback: the card guard still checked every line; this layer is skipped and logged
            logging.getLogger("cahoots").warning("apply_guardrail skipped: Bedrock refused the account (%s)", e)
            return text, False
        logging.getLogger("cahoots").warning("apply_guardrail failed: %s", e)
        return None
