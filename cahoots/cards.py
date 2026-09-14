"""Step 1 of the app: turn a pasted AI memory (ChatGPT / Claude) into a Card.

The raw text is read exactly once, distilled into the shareable Card, and never stored. Only the
Card (which the human edits and approves) exists afterwards.
"""

from __future__ import annotations

import os
import re

from strands import Agent

from cahoots.schemas import Card

MODEL_ID = os.environ.get("CAHOOTS_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

CARD_SYSTEM = """You are the onboarding step of Cahoots, an app whose agents get people out of the house
with people they'd actually like. You receive the text an AI assistant has memorised about a person
(their ChatGPT or Claude memory export). Distil it into a Card: the ONLY information that will ever be
shared with other people's agents. If the memory says openly what the person does well and would be happy to be asked
about (a trade, a skill, a door they can open), put it in can_help_with in their words. If it names their trade or job in
plain terms (a bookkeeper, a bike mechanic, a carpenter), can_help_with offers that skill ("bookkeeping for a small
business"), unless the work is health, legal or money-sensitive; otherwise leave it empty.
Never derive a card field from health, money, family, relationship or legal facts:
no "needs seated activities" from an injury, no lower budget from a debt, no "avoids alcohol" from
recovery, no "won't go to <neighbourhood>" because an ex lives or drinks there; the card holds only what
the person would state openly, and the private facts stay behind.

Rules:
- Keep only what helps plan a good night out: interests, hobbies, what they enjoy, what they avoid,
  where they live (neighbourhood level only), budget, when they're usually free, whether they're new in
  town, what they're looking for (friends / hobby / dates / anything).
- Drop everything else. NEVER put any of these on the card, in any field, even as a helpful note:
  health and medical facts (allergies, conditions, medication, therapy, diet for medical reasons),
  family or relationship situations, money beyond a rough outing budget, work or employer details,
  immigration status, religion, politics, sexuality unless they asked for it as a mode, addresses.
  If in doubt, leave it out. The person will edit the card and must not be embarrassed by it.
- one_liner is how other guests will see them, warm and specific: "Priya, the novelist", not "a user".
- Use the person's own words for notes_for_agents only if they are clearly fine to share.
- If the memory is thin, fill what you can and leave the rest minimal; do not invent facts.
- budget_cad: only a figure the person actually states for ONE night out; a weekly or monthly figure is not a per-night budget
  (a month is about four nights out: "$40 a month" is about $10 a night); never lower it because of debt, a lost job
  or money worries (those stay private); if nothing is stated, leave the default.
- Availability: infer only from explicit statements ("free most Saturdays"), otherwise default to
  Fri 18:00-23:00, Sat 12:00-23:00, Sun 12:00-20:00.
"""


def card_agent(model_id: str | None = None, region: str | None = None) -> Agent:
    from cahoots.person_agent import bedrock

    return Agent(
        model=bedrock(model_id or MODEL_ID, 0.2, region=region),
        system_prompt=CARD_SYSTEM,
        callback_handler=None,
    )


def extract_card(memory_text: str, person_id: str, first_name: str, neighbourhood_hint: str = "") -> Card:
    """Read the memory once and return the Card. The caller must discard `memory_text` after this."""
    prompt = (
        f"person_id: {person_id}\nfirst_name: {first_name}\n"
        f"neighbourhood_hint: {neighbourhood_hint or 'unknown, infer if stated'}\n\n"
        f"AI memory export follows between the markers.\n<memory>\n{memory_text.strip()}\n</memory>\n\n"
        "Produce the Card."
    )
    from cahoots.person_agent import ANTHROPIC_API_KEY, FALLBACK_MODEL_ID, MODEL_REGIONS, first_open_region, is_denied, is_throttled, mark_denied, mark_exhausted

    start = first_open_region()  # a region whose daily quota is known to be gone is not tried first again (the wake path learned this; the extractor now shares it)
    order = [start] + [r for r in MODEL_REGIONS if r != start]
    attempts = [(None, r) for r in order] + [(FALLBACK_MODEL_ID, None)]  # the main model in every open region, then the fallback
    if ANTHROPIC_API_KEY:
        attempts.append(("anthropic", None))  # and, if Bedrock refuses the account, the same model from Anthropic directly
    for i, (mid, region) in enumerate(attempts):
        try:
            card = card_agent(mid, region)(prompt, structured_output_model=Card).structured_output
            break
        except Exception as e:  # noqa: BLE001
            if ANTHROPIC_API_KEY and mid != "anthropic" and is_denied(e):
                mark_denied()
                card = card_agent("anthropic", None)(prompt, structured_output_model=Card).structured_output
                break
            if not is_throttled(e) or i + 1 == len(attempts):
                raise
            if region and ("per day" in str(e).lower() or "tokens per day" in str(e).lower()):
                mark_exhausted(region)
    card.person_id = person_id
    card.first_name = first_name
    return clean_card(card)


DERIVED = re.compile(  # a card field that could only come from a health, money or family fact
    r"\b(seated|sit-?down only|can'?t stand|standing for long|accessible venues?|wheelchair|mobility|step[- ]free|no stairs|ground[- ]floor only|alcohol[- ]free|no alcohol|non[- ]alcoholic|dry venues?|"
    r"budget[- ]conscious|tight budget|low[- ]cost only|cheap only|free events only|childcare|single parent|custody|"
    r"works? (nights?|days?|weekends?|evenings?|mornings?|shifts?|overnights?|late|early)|night shifts?|day shifts?|shift ?work\w*|on call|"
    r"(my |the |an? )?(ex|ex-?(boyfriend|girlfriend|partner|wife|husband)|former partner)\b.*|won'?t go (near|to|back to) [A-Z][\w' ]+|avoids? [A-Z][\w']+ (village|district|area|corridor)|breakup|divorce)\b", re.IGNORECASE)


def DERIVED_SPLIT(text: str) -> str:
    """Drop only the sentences that carry a derived limit."""
    return " ".join(x for x in re.split(r"(?<=[.!?])\s+", text) if x and not DERIVED.search(x)).strip()


FORBIDDEN = re.compile(
    # health
    r"allerg|celiac|gluten[- ]free for|diabet|asthma|medication|meds\b|lithium|insulin|inhaler|therap|psychiatr|"
    r"counsel(l)?or|anxiety|depress|bipolar|ptsd|ocd\b|adhd|autis|diagnos|surgery|chemo|cancer|hiv\b|std\b|"
    r"pregnan|miscarr|ivf\b|fertil|sober|recover|relapse|rehab|addict|disorder|chronic|injur|physio|"
    # family and relationships
    r"divorc|custody|estranged|grief|passed away|funeral|widow|ex[- ](husband|wife|partner|boyfriend|girlfriend)|"
    r"breakup|broke up|cheat|affair|"
    # money and work
    r"salary|\$\d{2,3},?\d{3}|debt|loan|bankrupt|evict|layoff|laid off|fired|unemploy|student budget|tight budget|on a budget|can't afford|"
    # where they came from
    r"moved (here |to \w+ )?from |new(ly)? arrived from |originally from |grew up in |"
    # immigration, religion, politics
    r"visa|permit|immigra|refugee|asylum|deport|"
    r"\b(christian|muslim|jewish|hindu|sikh|buddhist|atheist|catholic|mormon)\b|\b(liberal|conservative|ndp|tory)\b|"
    # addresses
    r"\d{2,5}\s+\w+\s+(st|street|ave|avenue|rd|road|blvd|crescent|cres|drive|dr)\b|apartment \d|unit \d",
    re.IGNORECASE,
)


HELP_SENSITIVE = re.compile(r"\b(health|medical|medic\w*|clinic\w*|nurs\w*|pediatric|paediatric|patients?|diagnos\w*|therap\w*|counsel\w*|mental|psych\w*|"
                            r"legal|law\b|lawyer|attorney|tenan\w*|evict\w*|immigra\w*|visa|custody|divorce|debt|bankrupt\w*|tax(?:es)?\b|insurance|benefits?\b|welfare|"
                            r"disabilit\w*|accessib\w*|deaf|blind|addict\w*|recovery|sober|rehab)\b", re.IGNORECASE)  # a trade in these fields is offered privately, by the agent, never on the public card


NEWCOMER = re.compile(r",?\s*(?:and\s+|but\s+|who\s+|who's\s+|is\s+|'s\s+|are\s+)?\b(?:new (?:to|in) (?:town|winnipeg|the city)|(?:just|recently|newly) moved(?: here| to \w+)?|newcomers? (?:to|in) \w+|still (?:meeting|getting to know) people)\b[^.;,]*", re.IGNORECASE)


def scrub_public_fields(card: Card, removed: list[str] | None = None) -> Card:
    """The card's other public strings get the same floor as interests: can_help_with loses any forbidden clause (a health word
    from the import is not a service), and no field says the person is new in town (a fact for their agent, never for a stranger)."""
    removed = removed if removed is not None else []
    help_ = card.can_help_with or ""
    if help_ and (FORBIDDEN.search(help_) or NEWCOMER.search(help_) or HELP_SENSITIVE.search(help_)):
        parts = [x.strip(" ,;") for x in re.split(r"(?<=[.;])\s+|,\s+(?=[a-z])", help_) if x.strip(" ,;")]
        kept = [x for x in parts if not FORBIDDEN.search(x) and not NEWCOMER.search(x) and not HELP_SENSITIVE.search(x)]
        removed += [x for x in parts if x not in kept]
        card.can_help_with = ("; ".join(kept) if kept else "").strip()
    for field in ("one_liner", "notes_for_agents"):
        v = getattr(card, field, "") or ""
        if NEWCOMER.search(v):
            removed.append(NEWCOMER.search(v).group(0).strip(" ,"))
            v = NEWCOMER.sub("", v)
            v = re.sub(r"\s{2,}", " ", v).strip(" ,;")
            v = re.sub(r"\(\s*\)", "", v).strip(" ,;")
            setattr(card, field, v)
    card.likes = [x for x in card.likes if not NEWCOMER.search(x)]
    card.interests = [x for x in card.interests if not NEWCOMER.search(x)]
    return card


_CARD_STOP = set("""winnipeg unknown person people friends friend hobby hobbies dates dating quiet lively nerdy creative music loud
nice good great love loves like likes enjoy enjoys into around near evening evenings night nights afternoon morning weekend
weekends someone anyone meeting meet things stuff want wants looking help helps happy would could really very much most more
also with from that this over under lowkey cafe cafes coffee dinner lunch walk board games game nights someone town city""".split())


def card_grounded(card: Card, memory: str) -> bool:
    """A card's distinctive content must come from THIS person's memory. A card whose one_liner, interests and
    can_help_with share almost none of their content words with the pasted memory did not come from it (a crossed
    or mismatched extraction) and must never be shown. Lenient (prefix-matched, low threshold) so a paraphrase
    passes; the point is to catch a card that is about someone else entirely. Returns True when there is too
    little to judge."""
    mem = {w for w in re.findall(r"[a-z]{4,}", (memory or "").lower())}
    if len(mem) < 8:
        return True  # too little memory to judge against; the human still approves the card
    mem_stems = {w[:5] for w in mem}
    words = []
    for part in [getattr(card, "one_liner", ""), getattr(card, "can_help_with", ""), getattr(card, "notes_for_agents", "")] + list(getattr(card, "interests", []) or []):
        words += re.findall(r"[a-z]{4,}", (part or "").lower())
    first = (getattr(card, "first_name", "") or "").lower()
    distinct = {w for w in words if w not in _CARD_STOP and w != first}
    if len(distinct) < 4:
        return True  # too little distinctive content to judge
    hit = sum(1 for w in distinct if w[:5] in mem_stems)
    return hit >= max(2, len(distinct) // 4)


def clean_card(card: Card) -> Card:
    """Second pass, deterministic floor: strip any interest, note sentence or one-liner that matches a
    forbidden category. The model is told not to include these; this makes it mechanical. It is a
    floor, not a guarantee: a private fact phrased in words the list does not know can pass, which is
    why the human edits and approves the card before it is used."""
    removed: list[str] = []
    keep = []
    for i in card.interests:
        (removed if FORBIDDEN.search(i) else keep).append(i)
    card.interests = keep  # an empty list beats re-inserting a forbidden interest
    scrub_public_fields(card, removed)
    for field in ("notes_for_agents", "one_liner"):
        v = getattr(card, field, "") or ""
        if DERIVED.search(v):
            setattr(card, field, DERIVED_SPLIT(v))
    card.likes = [x for x in card.likes if not DERIVED.search(x)]
    card.avoids = [x for x in card.avoids if not DERIVED.search(x)]
    if FORBIDDEN.search(card.notes_for_agents or ""):
        removed.append(card.notes_for_agents)
        sentences = re.split(r"(?<=[.!?])\s+", card.notes_for_agents)
        card.notes_for_agents = " ".join(s for s in sentences if not FORBIDDEN.search(s)).strip()
    if FORBIDDEN.search(card.one_liner):
        card.one_liner = f"{card.first_name}, into {card.interests[0]}" if card.interests else card.first_name
    card.asks = []  # asks come only from what the person types on their screen, never from the extraction
    card.neighbourhood = card.neighbourhood.strip()
    if FORBIDDEN.search(card.neighbourhood):  # a street address is not a neighbourhood
        card.neighbourhood = card.neighbourhood.split(",")[-1].strip() or "Winnipeg"
    return card


def card_to_text(card: Card) -> str:
    """What the person sees and edits: plain words, no JSON."""
    days = ", ".join(f"{a.day} {a.start}-{a.end}" for a in card.availability)
    return (
        f"{card.first_name}: {card.one_liner}\n"
        f"Into: {', '.join(card.interests)}\n"
        f"Good night: {', '.join(card.likes)}. Bad night: {', '.join(card.avoids) or 'nothing in particular'}\n"
        f"Open to: {', '.join(card.modes)}\n"
        f"Around: {card.neighbourhood}, up to {card.max_travel_min} min away, about ${card.budget_cad}\n"
        f"Group: {card.group_size_pref[0]} to {card.group_size_pref[1]} people\n"
        f"Free: {days}\n"
        + (f"Also: {card.notes_for_agents}\n" if card.notes_for_agents else "")
    )


if __name__ == "__main__":
    import sys

    text = sys.stdin.read()
    c = extract_card(text, person_id="demo", first_name=sys.argv[1] if len(sys.argv) > 1 else "Jashan")
    print(card_to_text(c))
    print(c.model_dump_json(indent=2))
