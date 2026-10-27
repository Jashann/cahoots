"""Agreement and hold rails, each pinned to the live line that produced it."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))


def test_a_question_about_our_own_person_is_not_their_yes():
    from cahoots.social import AGREES, STRONG_AGREES

    line = "Just to clarify - which time are you asking about? Let me know which works for Quill and I can confirm for Dorothy"
    assert not AGREES.search(line)
    assert not STRONG_AGREES.search(line)
    assert AGREES.search("Saturday at 2 works for Dorothy")  # a plain yes about their own person still counts
    assert STRONG_AGREES.search("Saturday at 2 works perfectly for Dorothy")


def test_the_pre_tap_hold_keeps_the_time_it_is_holding():
    import cahoots.social as soc
    from cahoots.wire import _hold_pre_tap_yes

    soc.has_tap = lambda fs, about=None: False
    sender = type("S", (), {"simulated": False, "first_name": "Quill", "fs": None})()
    out = _hold_pre_tap_yes(sender, "Great! Saturday 19 September at 2 PM works perfectly for Quill. Dorothy and Yuki, are both your people in for that time?")
    assert "I'll check with Quill" in out
    assert "2 PM" in out and "Saturday" in out  # README rule 6: times and places stay
    assert "works perfectly for Quill" not in out


def test_a_multi_line_message_with_no_yes_is_not_logged_as_held():
    import cahoots.social as soc
    from cahoots.wire import _hold_pre_tap_yes

    soc.has_tap = lambda fs, about=None: False
    sender = type("S", (), {"simulated": False, "first_name": "Quill", "fs": None})()
    msg = "Hi there.\n\nI wanted to ask about board games.\n\nAre you free Thursday?"
    assert _hold_pre_tap_yes(sender, msg) == " ".join(msg.split())  # the caller compares against the normalised text


def test_a_tap_does_not_carry_to_the_same_weekday_a_week_later():
    from cahoots.social import _words_name_plan

    t = datetime.fromisoformat("2026-09-20T15:00")
    assert _words_name_plan("Quill is in for Sunday.", t, "")
    assert not _words_name_plan("Quill is in for Sunday the 27th.", t, "")
    assert _words_name_plan("Quill is in for Sunday the 20th.", t, "")


def test_the_guard_puts_a_lost_plan_back_as_words_not_tokens():
    from cahoots.guard import _plan_phrase, _plan_tokens

    assert _plan_phrase(_plan_tokens("Saturday 19 September at 2 PM works")) == "Saturday 19 September at 2 PM"
    assert "," not in _plan_phrase(["Sunday", "3 PM", "1:30"])


def test_ordinary_english_no_longer_throws_away_the_agents_answer():
    from app import _HARNESS_WORDS, _off_screen

    refusal = "I can work around your night shifts. I won't tell anyone about your health or your job."
    assert not _HARNESS_WORDS.search(refusal)
    kept, dropped = _off_screen("That failed with a system error on my side. I can still look for board games this week.")
    assert kept == "I can still look for board games this week."
    assert "system error" in dropped


def test_the_edge_of_someones_hours_is_not_a_time_on_offer():
    from app import _offered_clauses

    line = "That's outside Olga's availability (free afternoons, ends at 6 PM). Wednesday afternoon would work though - how about 2 PM at Thom Bargen that day, September 16th?"
    offered = _offered_clauses(line)
    assert "6 PM" not in offered
    assert "2 PM" in offered
    assert "Thursday 6 works" in _offered_clauses("Can't do Tuesday 3 PM, but Thursday 6 works")


def test_a_settled_question_cannot_be_answered_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-test51")
    q = soc.push_prompt(fs, "question", "Sat 19 Sep, 14:00 at Forth with Dorothy. In?", options=["In", "No"], log=False)
    assert soc.withdraw_prompt(fs, q["id"])
    assert soc.answer_prompt(fs, q["id"], "In") is None  # the button is still on an open page; it may not book
    assert next(x for x in soc.load_prompts(fs) if x["id"] == q["id"])["answer"] == "withdrawn"


def test_the_card_guard_now_runs_on_the_agent_that_holds_the_private_notes():
    from cahoots.guard import CardGuard
    from cahoots.social import WIRE_TOOLS

    g = CardGuard(terms_fn=lambda: ["night-shift nurse"], only_tools=WIRE_TOOLS)
    msg = {"role": "assistant", "content": [
        {"toolUse": {"name": "dm", "input": {"message": "She is a night-shift nurse, so evenings are hard."}}},
        {"toolUse": {"name": "write", "input": {"text": "night-shift nurse"}}},
        {"text": "I'll tell them about the night-shift nurse hours."},
    ]}
    g.on_message(type("E", (), {"message": msg})())
    assert "night-shift nurse" not in msg["content"][0]["toolUse"]["input"]["message"]  # what leaves is scrubbed
    assert msg["content"][1]["toolUse"]["input"]["text"] == "night-shift nurse"  # its own notes are untouched
    assert "night-shift nurse" in msg["content"][2]["text"]  # so is what it says to its own person


def test_replay_protection_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.wire as w

    w._SEEN_NONCES.clear(); w._SEEN_LOADED = False
    assert w._consume_nonce("abc123", "u-1")
    assert not w._consume_nonce("abc123", "u-1")
    w._SEEN_NONCES.clear(); w._SEEN_LOADED = False  # a container swap: memory is gone, the host volume is not
    assert not w._consume_nonce("abc123", "u-1")
    assert w._consume_nonce("abc123", "u-2")  # each recipient gets its own envelope

