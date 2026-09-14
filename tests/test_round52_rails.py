"""Round-52 findings, each pinned to the live line that produced it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))


def _thread(fs, tid, lines):
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    (fs.root / "threads" / f"{tid}.md").write_text("\n".join(lines) + "\n")


def test_an_exception_question_may_only_report_a_time_the_other_side_named(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t52a")
    monkeypatch.setattr(soc, "_pids_for_name", lambda fs_, name: ["p001"])
    now = soc.local_now().strftime("%Y-%m-%d %H:%M")
    _thread(fs, "t-aaa111", [f"[{now}] p001 -> u-t52a: Which time were you thinking? Yuki could also do Thursday."])
    assert not soc._named_time_on_thread(fs, "Yuki", "2026-09-18T19:00")  # a question naming another day is not "Yuki can only do Friday 19:00"
    _thread(fs, "t-aaa222", [f"[{now}] p001 -> u-t52a: Yuki can only do Friday September 18th at 7 PM, I'm afraid."])
    assert soc._named_time_on_thread(fs, "Yuki", "2026-09-18T19:00")


def test_a_conditional_that_names_a_time_is_still_not_a_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t52b")
    monkeypatch.setattr(soc, "_pids_for_name", lambda fs_, name: ["p002"])
    monkeypatch.setattr(soc, "load_card", lambda fs_: None)
    now = soc.local_now().strftime("%Y-%m-%d %H:%M")
    _thread(fs, "t-bbb111", [f"[{now}] p002 -> u-t52b: Assuming the weather holds, Tuesday 7 PM at Forth is perfect for Marnie."])
    reasons = soc._not_agreed(fs, "Marnie", plan_when="2026-09-15T19:00", plan_where="Forth")
    assert any("conditional" in r for r in reasons)
    _thread(fs, "t-bbb111", [f"[{now}] p002 -> u-t52b: Tuesday 7 PM at Forth works perfectly for Marnie, count her in."])
    assert soc._not_agreed(fs, "Marnie", plan_when="2026-09-15T19:00", plan_where="Forth") == []


def test_a_note_about_someone_is_hidden_once_a_question_about_them_is_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t52c")
    monkeypatch.setattr(soc, "_network_index", lambda: {"p039": "Patricia"})
    note = soc.push_prompt(fs, "note", "I need to know who Patricia is before I can arrange anything.", log=False)
    soc.push_prompt(fs, "question", "Patricia can only do Saturday at 15:00. Make an exception this once?", ["Yes, this once", "No, keep my times"], log=False)
    assert next(q for q in soc.load_prompts(fs) if q["id"] == note["id"])["answer"] == "seen"


def test_a_rails_own_wording_never_reaches_the_screen_as_the_agents_words():
    from app import _off_screen

    kept, dropped = _off_screen("I didn't go with that one. Saturday is outside Dorian's free windows (sun 12:00-18:00). I'll look for a Sunday instead.")
    assert "free windows" not in kept
    assert kept == "I didn't go with that one. I'll look for a Sunday instead."


def test_a_roster_of_people_off_the_thread_is_a_report_about_them(monkeypatch):
    import cahoots.wire as w

    monkeypatch.setattr("cahoots.social._network_index", lambda: {"p001": "Harold", "p002": "Olga", "p003": "Tobin", "p004": "Quinlan", "u-me": "Dorian", "p009": "Sasha"})
    sender = type("S", (), {"person_id": "u-me", "first_name": "Dorian"})()
    text = "Sounds good. So far it's Quinlan, Harold, Olga (pending final confirm), and Tobin. Try Sasha's agent for the venue."
    kept, dropped = w._third_party_scrub(sender, ["p009"], text)
    assert dropped == 1
    assert "Quinlan" not in kept and "Try Sasha's agent" in kept
