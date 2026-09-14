"""Generate the simulated city: N residents of Winnipeg with realistic Cards.

Why simulated: a network app needs people on day one. We say this openly in the README and video.
Real users join the same pool. Personas are generated once and committed to data/personas.json.

Usage: .venv/bin/python scripts/make_personas.py 60
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

from pydantic import BaseModel
from strands import Agent
from strands.models.bedrock import BedrockModel

from cahoots.schemas import Card

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = os.environ.get("CAHOOTS_PERSONA_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

NEIGHBOURHOODS = [
    "Exchange District", "Osborne Village", "Wolseley", "St. Boniface", "River Heights", "West End",
    "Fort Garry", "St. Vital", "North Kildonan", "Downtown", "Corydon", "Transcona", "Tuxedo",
]

SEED_ARCHETYPES = [
    "new-in-town software developer who reads sci-fi and plays pickup basketball",
    "nurse on rotating shifts who loves live music and hates small talk",
    "retired teacher who runs a book club and wants to try board-game cafés",
    "grad student in history, broke, competitive at trivia",
    "single dad, free every other weekend, into cycling and cheap eats",
    "novelist working on a second book, quiet bars, dislikes crowds",
    "recent immigrant accountant looking for friends, loves cooking and cricket",
    "hairstylist with big weekend energy, karaoke, dancing, anything loud",
    "birder and hiker, early riser, would rather walk than drink",
    "line cook off on Mondays, wants people who eat adventurously",
    "high-school coach, basketball and beer, Saturday afternoons",
    "illustrator who wants sketching meetups and gallery nights",
    "two-jobs warehouse worker with only Sunday afternoons free",
    "PhD dropout turned barista, philosophy talks, chess",
    "nervous first-time dater in their 30s, prefers group settings",
]


class PersonaBatch(BaseModel):
    cards: list[Card]


SYSTEM = """You invent realistic residents of Winnipeg, Canada for a social-planning simulation.
Each Card must feel like a specific real person: concrete interests (not 'music' but 'punk shows at
the Good Will'), a warm one_liner, honest avoids, plausible availability for their job, budgets from
$10 to $80. Mix ages 22 to 70, genders, cultures (Winnipeg is Filipino, Punjabi, Indigenous,
Ukrainian, Nigerian, Chinese, French-Canadian and more). Vary modes: most 'friends' or 'hobby',
some 'dates', some 'anything'. No two people alike. Use the given person_ids and first names.
Availability must be realistic for going out: most people list Fri 18:00-23:00 and Sat 18:00-23:00
and Sun 12:00-18:00 (at least two of those three), some add a weekday evening, shift workers differ.
Give every person at least two windows of 4+ hours. First names must all be different from each other.
"""


def main(n: int) -> None:
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION, temperature=0.9),
        system_prompt=SYSTEM,
        callback_handler=None,
    )
    out: list[dict] = []
    batch = 6
    rnd = random.Random(7)
    i = 0
    while len(out) < n:
        ids = [f"p{i + k:03d}" for k in range(batch)]
        i += batch
        arche = rnd.sample(SEED_ARCHETYPES, k=min(batch, len(SEED_ARCHETYPES)))
        hoods = [rnd.choice(NEIGHBOURHOODS) for _ in ids]
        prompt = "Create these people:\n" + "\n".join(
            f"- person_id {pid}: {a}; lives around {h}; invent a first name that fits"
            for pid, a, h in zip(ids, arche, hoods)
        )
        result = agent(prompt, structured_output_model=PersonaBatch).structured_output
        for c in result.cards:
            out.append(c.model_dump(mode="json"))
        print(f"{len(out)}/{n}", file=sys.stderr)
    (ROOT / "data" / "personas.json").write_text(json.dumps(out[:n], indent=2, ensure_ascii=False))
    print(f"wrote {min(len(out), n)} personas to data/personas.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
