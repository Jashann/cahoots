"""Long-term memory per person, on Amazon Bedrock AgentCore Memory.

What goes in, per actor (person_id):
  - the approved Card (as a "card approved" event)
  - what the person says on the one screen, including "how was it?" answers (record_text)
  - every invite reply (in / not this one / not this week)
AgentCore's user-preference and semantic strategies distill those events into memory records
("prefers small groups", "liked Priya", "found the café too loud"). Before an agent decides for its
human, it pulls the top records and folds them into the private history the Card never exposes.

Local JSON under work/ remains the source of truth for the hackathon build (so the demo never depends
on a network call); AgentCore Memory is the persistence layer the deployed version reads first.
"""

from __future__ import annotations

import os
from pathlib import Path

from cahoots.schemas import Card, OutingFeedback

ROOT = Path(__file__).resolve().parents[1]
REGION = os.environ.get("AWS_REGION", "us-east-1")


def memory_id() -> str | None:
    mid = os.environ.get("CAHOOTS_MEMORY_ID")
    if mid:
        return mid
    for p in (ROOT / "work" / "memory_id.txt", ROOT / "data" / "memory_id.txt"):
        if p.exists():
            return p.read_text().strip()
    return None


_SHARED: "PeopleMemory | None" = None


def shared() -> "PeopleMemory":
    """One MemoryClient per process. Sixty agents must not mean sixty clients."""
    global _SHARED
    if _SHARED is None:
        _SHARED = PeopleMemory()
    return _SHARED


class PeopleMemory:
    """Thin wrapper; every method is safe to call when memory is not configured (it just no-ops)."""

    def __init__(self, mid: str | None = None):
        self.mid = mid or memory_id()
        self._client = None
        self._touched: set[str] = set()  # persona ids that have written an event this process
        if self.mid:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=REGION)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _event(self, actor: str, session: str, user_text: str, agent_text: str) -> None:
        if not self._client:
            return
        self._touched.add(actor)
        self._client.create_event(
            memory_id=self.mid, actor_id=actor, session_id=session,
            messages=[(user_text, "USER"), (agent_text, "ASSISTANT")],
        )

    def record_card(self, card: Card) -> None:
        self._event(
            card.person_id, "onboarding",
            f"Here is my approved card: {card.model_dump_json()}",
            f"Saved. I'll only share this card with other agents when planning for {card.first_name}.",
        )

    def record_text(self, person_id: str, text: str, session: str = "talks") -> None:
        """What the person said to their agent on the one screen (an answer, a question, feedback)."""
        self._event(person_id, session, text, "Noted; I'll keep that in mind.")

    def recall(self, person_id: str, query: str = "what makes a good night out for this person", k: int = 6) -> list[str]:
        """Top memory records for a person, as plain strings. Empty list if memory is off or cold.
        Simulated residents (ids p000..p059) never onboarded, so they have no actor in Memory; skip the
        round trip for them unless they have given feedback through the app."""
        if not self._client:
            return []
        if person_id.startswith("p") and person_id[1:].isdigit() and person_id not in self._touched:
            return []
        out: list[str] = []
        for ns in (f"/people/{person_id}/prefs", f"/people/{person_id}/facts"):
            try:
                recs = self._client.retrieve_memories(memory_id=self.mid, namespace=ns, query=query, top_k=k)
            except Exception as e:  # noqa: BLE001  (cold namespaces raise; log at debug and carry on)
                import logging

                (logging.getLogger("cahoots").warning if "AccessDenied" in str(e) or "Throttl" in str(e) else logging.getLogger("cahoots").debug)("memory retrieve %s: %s", ns, e)
                print("memory recall failed:", ns, type(e).__name__, str(e)[:120])  # visible in the runtime log; a cold namespace is quiet, a denied one is not
                recs = []
            for r in recs:
                text = (r.get("content") or {}).get("text") if isinstance(r, dict) else None
                if text:
                    out.append(_plain(text))
        return out[:k]


def _plain(text: str) -> str:
    """AgentCore's strategies store records as JSON ({"context": ..., "preference": ..., "categories": [...]});
    keep the sentence, drop the wrapper, so downstream checks see words about the person, not field names."""
    import json as _json

    t = text.strip()
    if t.startswith("{"):
        try:
            d = _json.loads(t)
            for k in ("preference", "fact", "text", "content", "context"):  # the sentence about the person, not the wrapper
                if d.get(k):
                    return str(d[k])
        except ValueError:
            pass
    return t
