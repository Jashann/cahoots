"""Create the AgentCore Memory resource Cahoots expects, in your own account, and print its id.

  AWS_REGION=us-east-1 .venv/bin/python scripts/create_memory.py [--name cahoots_people]
  export CAHOOTS_MEMORY_ID=<printed id>     # or write it to work/memory_id.txt

Two long-term strategies, with the namespaces cahoots/memory.py queries:
  user preferences  -> /people/{actorId}/prefs
  semantic facts    -> /people/{actorId}/facts
Events are written per person (actor_id = person_id) by PeopleMemory.record_card / record_reply /
record_feedback; the strategies distil them into records the agents recall into their notes.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from bedrock_agentcore.memory import MemoryClient

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="cahoots_people")
    ap.add_argument("--write", action="store_true", help="also write the id to work/memory_id.txt")
    a = ap.parse_args()
    client = MemoryClient(region_name=os.environ.get("AWS_REGION", "us-east-1"))
    mem = client.create_memory_and_wait(
        name=a.name,
        description="Cahoots: per-person cards, invite replies and 'How was it?' feedback",
        strategies=[
            {"userPreferenceMemoryStrategy": {"name": "prefs", "namespaces": ["/people/{actorId}/prefs"]}},
            {"semanticMemoryStrategy": {"name": "facts", "namespaces": ["/people/{actorId}/facts"]}},
        ],
        event_expiry_days=90,
    )
    mid = mem["id"] if isinstance(mem, dict) else getattr(mem, "id", str(mem))
    print(mid)
    if a.write:
        (ROOT / "work").mkdir(exist_ok=True)
        (ROOT / "work" / "memory_id.txt").write_text(mid + "\n")
        print("written to work/memory_id.txt")


if __name__ == "__main__":
    main()
