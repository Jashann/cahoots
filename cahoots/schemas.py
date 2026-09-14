"""Data shapes shared by every agent in Cahoots.

Everything an agent says about a person to another agent goes through `Card`. Nothing else about a
person ever crosses the network. Keep this file boring and explicit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Vibe = Literal["quiet", "lively", "outdoors", "competitive", "creative", "food", "music", "nerdy"]
Mode = Literal["friends", "hobby", "dates", "anything"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def normalise_time(t: str) -> str:
    """'7:00 PM' / '7pm' / '9:00' / '19' -> 'HH:MM'. Models and humans are not consistent; the scheme
    does arithmetic on these strings, so they are normalised once, here, or rejected."""
    import re

    raw = (t or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", raw)
    if not m:
        raise ValueError(f"unreadable time {t!r}; use 24h like '19:00'")
    h, mnt, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h == 24 and mnt == 0:
        h = 0
    if not (0 <= h <= 23 and 0 <= mnt <= 59):
        raise ValueError(f"time out of range {t!r}")
    return f"{h:02d}:{mnt:02d}"


class Availability(BaseModel):
    day: Weekday
    start: str = Field(description="24h local time, e.g. '18:00'")
    end: str = Field(description="24h local time, e.g. '23:00'")

    @field_validator("start", "end", mode="before")
    @classmethod
    def _norm(cls, v):
        return normalise_time(str(v))


class Card(BaseModel):
    """The shareable card. This is the ONLY thing a Person Agent may reveal to other agents."""

    person_id: str
    first_name: str
    one_liner: str = Field(description="How other guests will see this person, e.g. 'Priya, the novelist'")
    interests: list[str] = Field(description="Short tags, e.g. ['sci-fi novels', 'pickup basketball']")
    likes: list[Vibe] = Field(description="Vibes that make a good night")
    avoids: list[Vibe] = Field(default_factory=list, description="Vibes that make a bad night")
    modes: list[Mode] = Field(description="What they're open to")
    neighbourhood: str
    max_travel_min: int = 25
    budget_cad: int = Field(25, description="Comfortable spend for one outing (25 when the memory states none)")
    group_size_pref: tuple[int, int] = Field(default=(4, 8), description="(min, max) people incl. self")
    availability: list[Availability]
    notes_for_agents: str = Field(
        default="", description="Anything else the person approved sharing, in their words"
    )
    asks: list[str] = Field(default_factory=list, description="What the person asked their agent to find for them, in their words (a manufacturer, a mentor): public by construction, they typed it as a request to find someone")
    can_help_with: str = Field(
        default="", description="What this person can offer others and is happy to be asked about, e.g. 'small-batch garment manufacturing; knows every fabric supplier in town'"
    )


class PrivateProfile(BaseModel):
    """What the Person Agent keeps for its own human. Never leaves the agent."""

    person_id: str
    card: Card
    history: list["OutingFeedback"] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list, description="Venues/people the human said no to")


class Venue(BaseModel):
    venue_id: str
    name: str
    neighbourhood: str
    vibes: list[Vibe]
    capacity_hint: int
    price_level: Literal[1, 2, 3]
    address: str
    why: str = Field(description="One line on what it's good for")


class Outing(BaseModel):
    """A plan the Host Agent is assembling. Becomes invites once the group is set."""

    outing_id: str
    host_person_id: str
    venue: Venue
    starts_at: datetime
    duration_min: int
    vibe: list[Vibe]
    mode: Mode
    size_target: tuple[int, int]
    pitch: str = Field(description="One sentence a human would enjoy reading")
    confirmed: list[str] = Field(default_factory=list, description="person_ids who tapped In")
    invited: list[str] = Field(default_factory=list)
    status: Literal["forming", "confirmed", "cancelled", "done"] = "forming"


class Invite(BaseModel):
    """The one message a human sees."""

    invite_id: str
    outing_id: str
    person_id: str
    text: str = Field(
        description="Plain, specific: when, where, how many, two things they'd care about, 'In?'"
    )
    guests_preview: list[str] = Field(description="First name + one-liner for confirmed guests")
    reply: Literal["in", "not_this_one", "not_this_week", None] = None
    replied_at: datetime | None = None


class OutingFeedback(BaseModel):
    outing_id: str
    person_id: str
    rating: Literal[1, 2, 3, 4, 5]
    words: str = Field(description="The one sentence they said")
    would_see_again: list[str] = Field(default_factory=list, description="person_ids, only if they said so")
    learned: list[str] = Field(default_factory=list, description="Card adjustments the agent inferred")
