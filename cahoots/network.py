"""Wire helpers shared by the v2 network (cahoots/wire.py): a Strands Agent that always runs with
per-invocation limits, and the parsing of what comes back over A2A.

CappedAgent: the A2A executor calls stream_async without limits, and these agents face the public
internet, so every invocation carries WIRE_LIMITS. agent_text: only what the remote AGENT said (the
task's artifacts, then agent-role messages), never our own echoed message. parse_reply /
_json_objects: the JSON object in a streamed, possibly chatty reply, defaulting to a decline.
"""


from __future__ import annotations


import json


from strands import Agent


WIRE_LIMITS = {"turns": 6, "output_tokens": 3000, "total_tokens": 60000}


class CappedAgent(Agent):
    """A Strands Agent whose every invocation carries `limits`. The A2A executor calls stream_async
    without limits, and these agents face the public internet; the inner agents that a DM can wake get
    their own (larger) caps so a crafted message cannot drive an unbounded loop."""

    def __init__(self, *args, limits: dict | None = None, **kwargs):
        self._limits = dict(limits or WIRE_LIMITS)
        super().__init__(*args, **kwargs)

    def stream_async(self, *args, **kwargs):  # type: ignore[override]
        if kwargs.get("limits") is None:
            kwargs["limits"] = dict(self._limits)
        return super().stream_async(*args, **kwargs)


def _last_user_text(agent) -> str:
    """The most recent user message WITH TEXT in the wire-facing agent's conversation: the proposal as
    it arrived over A2A. Tool results also travel as user-role messages (toolResult blocks, no text),
    so those are skipped; a second tool call still finds the proposal. Read here so the model never
    has to echo a long JSON payload as a tool argument."""
    for msg in reversed(getattr(agent, "messages", []) or []):
        if msg.get("role") != "user":
            continue
        text = "".join(b.get("text", "") for b in msg.get("content", []) if isinstance(b, dict) and "text" in b)
        if text.strip():
            return text
    return ""


def _texts(o):
    if isinstance(o, dict):
        if o.get("kind") == "text" and "text" in o:
            yield o["text"]
        for v in o.values():
            yield from _texts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _texts(v)


def agent_text(resp) -> str:
    """Only what the remote AGENT said: the task's artifacts first (an A2A artifact is a dict with
    `artifactId` and `parts`; it carries no `kind`), then messages with role=agent. Never the echoed
    user message (the A2A task carries our own proposal in its history)."""

    def walk(o):
        if isinstance(o, dict):
            yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)

    arts, agent_msgs = [], []
    for o in walk(resp):
        if ("artifactId" in o or o.get("kind") == "artifact") and "parts" in o:
            arts.append("".join(_texts(o["parts"])))
        elif o.get("role") == "agent" and "parts" in o:
            agent_msgs.append("".join(_texts(o["parts"])))
    if arts and any(a.strip() for a in arts):
        return "".join(arts)  # streamed chunks of one final message
    if agent_msgs:
        return agent_msgs[-1]  # the final answer, never the pre-tool commentary that came before it
    return "".join(t for t in _texts(resp) if "Outing proposal for your human" not in t)


def _json_objects(text: str):
    """Every top-level JSON object in text, nested braces included (raw_decode from each '{')."""
    dec = json.JSONDecoder()
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return
        try:
            obj, end = dec.raw_decode(text, i)
            yield obj
            i = end
        except json.JSONDecodeError:
            i += 1


def parse_reply(text: str) -> dict:
    """Pull the decision JSON out of an agent's reply. Handles nested objects (the counter) and prose or
    <thinking> wrappers. Falls back to a decline with the text as reason, never to a guessed accept."""
    import re

    text = re.sub(r"<\s*thinking\s*>.*?<\s*/\s*thinking\s*>", "", text, flags=re.S)
    text = text.replace("```json", "").replace("```", "")
    found = [o for o in _json_objects(text) if isinstance(o, dict) and "decision" in o]
    if found:
        d = found[-1]
        if d.get("decision") not in ("accept", "decline", "counter"):
            d["decision"] = "decline"
        if d.get("decision") == "counter" and not isinstance(d.get("counter"), dict):
            d["decision"] = "decline"
        return d
    return {"decision": "decline", "reason": text.strip()[:300] or "(no parseable reply)", "fit": 3, "counter": None, "unparsed": True}
