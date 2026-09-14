"""CardGuard must redact private phrases before an assistant message leaves the agent."""
from strands.hooks import MessageAddedEvent

from cahoots.guard import REDACTED, CardGuard
from cahoots.schemas import Availability, Card, OutingFeedback, PrivateProfile


def _profile() -> PrivateProfile:
    card = Card(person_id="p1", first_name="Ada", one_liner="Ada, the quiet novelist", interests=["novels"], likes=["quiet"],
                modes=["friends"], neighbourhood="Wolseley", budget_cad=30, availability=[Availability(day="sat", start="18:00", end="23:00")])
    prof = PrivateProfile(person_id="p1", card=card)
    prof.history.append(OutingFeedback(outing_id="o1", person_id="p1", rating=2, words="the café was far too loud for me honestly", would_see_again=[]))
    prof.do_not_repeat.append("Sargent Park Courts")
    return prof


def test_redacts_feedback_and_do_not_repeat():
    prof = _profile()
    guard = CardGuard(prof)
    msg = {"role": "assistant", "content": [{"text": "Ada said the café was far too loud for me honestly, and she avoids Sargent Park Courts."}]}
    guard.on_message(MessageAddedEvent(agent=None, message=msg))  # type: ignore[arg-type]
    text = msg["content"][0]["text"]
    assert "far too loud" not in text and "Sargent Park Courts" not in text
    assert REDACTED in text and len(guard.caught) >= 2


def test_leaves_card_facts_alone():
    prof = _profile()
    guard = CardGuard(prof)
    msg = {"role": "assistant", "content": [{"text": "Ada likes quiet places and novels; free Saturday evenings."}]}
    guard.on_message(MessageAddedEvent(agent=None, message=msg))  # type: ignore[arg-type]
    assert msg["content"][0]["text"] == "Ada likes quiet places and novels; free Saturday evenings."
    assert guard.caught == []
