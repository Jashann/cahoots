"""The AgentCore Runtime entrypoint. Three actions:

  {"action": "card", "first_name": "...", "memory": "<pasted AI memory>", "neighbourhood": "..."}
      -> the shareable Card (the memory text is not stored here)
  {"action": "reflect", "person_id": "...", "files": {<the agent's folder as {path: text}>}}
      -> the person's own agent wakes on the Runtime with its folder, reads the day's talks and threads,
         distils the import, updates me.md and MEMORY.md, and returns the changed files. No DMs from here:
         reflection touches only the person's own folder, so it is safe to run anywhere.
  {"action": "wake", "person_id": "...", "reason": "...", "payload": "...", "files": {...}, "registry": {...}}
      -> a full wake on the Runtime: the agent may DM other agents through the web app's public A2A URLs
         (registry) and returns its words plus the changed files.

Locally: .venv/bin/python agentcore_app.py    Deployed: agentcore deploy
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("CAHOOTS_WORK_DIR", "/tmp/cahoots-work")  # /var/task is read-only on the Runtime

from bedrock_agentcore.runtime import BedrockAgentCoreApp

if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("AGENT_OBSERVABILITY_ENABLED"):
    try:
        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
    except Exception as _e:  # noqa: BLE001
        print("telemetry not set up:", _e)

from cahoots.agentfs import AgentFS
from cahoots.cards import extract_card

app = BedrockAgentCoreApp()


def _changed(before: dict, fs: AgentFS) -> dict:
    after = fs.snapshot()
    return {k: v for k, v in after.items() if before.get(k) != v}


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    action = payload.get("action", "card")
    if action == "card":
        card = extract_card(payload["memory"], person_id=payload.get("person_id", "u-runtime"), first_name=payload["first_name"],
                            neighbourhood_hint=payload.get("neighbourhood", ""))
        return {"card": card.model_dump(mode="json")}
    if action in ("reflect", "wake"):
        from cahoots import wire
        from cahoots.social import Social

        pid = payload["person_id"]
        fs = AgentFS(pid)
        fs.restore(payload.get("files", {}))
        before = fs.snapshot()
        wire.REGISTRY.update(payload.get("registry", {}))
        me = Social(pid)
        n_recalled = 0
        if action == "reflect":
            recalled = ""
            try:
                from cahoots.memory import shared

                recs = shared().recall(pid, query="what this person said, wants and avoids", k=8)
                n_recalled = len(recs)
                recalled = ("\n\nAgentCore Memory recalled about them (their own words, distilled):\n" + "\n".join(f"- {r}" for r in recs)) if recs else ""
            except Exception as _e:  # noqa: BLE001
                print("memory recall skipped:", _e)
            words = me.wake("daily", "Reflection only: read me/import.md, today's talks and threads; write me.md (traits, what lights them up, what they avoid, what they're after, three guesses to test) and update MEMORY.md." + recalled, quiet=True)
        else:
            words = me.wake(payload.get("reason", "daily"), payload.get("payload", ""))
        return {"words": words, "files": _changed(before, fs), "recalled": n_recalled}
    return {"error": f"unknown action {action}", "actions": ["card", "reflect", "wake"]}


if __name__ == "__main__":
    app.run()
