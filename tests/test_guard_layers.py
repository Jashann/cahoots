"""Guard on tool payloads, time padding, midnight windows."""
from strands.hooks import MessageAddedEvent

from cahoots.guard import REDACTED, CardGuard
from cahoots.schemas import Availability, Card, OutingFeedback, PrivateProfile


def _prof():
    card = Card(person_id="p1", first_name="Ada", one_liner="Ada, the quiet novelist", interests=["novels"], likes=["quiet"],
                modes=["friends"], neighbourhood="Wolseley", budget_cad=30, availability=[Availability(day="sat", start="18:00", end="23:00")])
    prof = PrivateProfile(person_id="p1", card=card)
    prof.history.append(OutingFeedback(outing_id="o", person_id="p1", rating=2, words="Greg talked over everyone the whole night"))
    return prof


def test_guard_redacts_tool_use_payload():
    g = CardGuard(_prof())
    msg = {"role": "assistant", "content": [{"toolUse": {"name": "ProposalReply", "toolUseId": "x", "input": {"decision": "decline", "reason": "Ada said Greg talked over everyone the whole night"}}}]}
    g.on_message(MessageAddedEvent(agent=None, message=msg))  # type: ignore[arg-type]
    assert REDACTED in msg["content"][0]["toolUse"]["input"]["reason"] and "talked over" not in msg["content"][0]["toolUse"]["input"]["reason"]
