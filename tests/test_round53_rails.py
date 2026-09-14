"""Round-53 findings, each pinned to the live line that produced it."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))


def _thread(fs, tid, lines):
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    (fs.root / "threads" / f"{tid}.md").write_text("\n".join(lines) + "\n")


def test_the_mechanical_ask_stops_when_either_side_has_closed_the_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import app
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t53a")
    _thread(fs, "t-4950fb", [
        "[2026-09-14 11:05] Theo (me) -> p041: Free Monday evening around 7:30 PM. Thinking Assiniboine Park for a walk, quiet.",
        "[2026-09-14 11:06] p041 -> u-t53a: But a quiet park walk isn't the right fit. Rielle's after lively and music-focused. How about a live music spot Monday 7:30 PM instead?",
        "[2026-09-14 11:07] Theo (me) -> p041: Theo's after quiet nights. Not this time, but thanks for considering it!",
        "[2026-09-14 11:07] p041 -> u-t53a: No response needed, Theo's all set.",
    ])
    assert app._mechanical_ask("u-t53a", "t-4950fb") is False
    _thread(fs, "t-4950fc", [
        "[2026-09-14 11:05] Theo (me) -> p041: Free Monday evening around 7:30 PM. Thinking Assiniboine Park for a walk, quiet.",
        "[2026-09-14 11:06] p041 -> u-t53a: But a quiet park walk isn't the right fit. How about a live music spot Monday 7:30 PM instead?",
    ])
    assert app._mechanical_ask("u-t53a", "t-4950fc") is False  # the time may be theirs; the place they refused is not, and "as before" would say it was


def test_a_holding_line_with_a_courtesy_is_holding_not_a_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t53b")
    monkeypatch.setattr(soc, "_pids_for_name", lambda fs_, name: ["p007"])
    monkeypatch.setattr(soc, "load_card", lambda fs_: None)
    now = soc.local_now().strftime("%Y-%m-%d %H:%M")
    _thread(fs, "t-ccc111", [f"[{now}] p007 -> u-t53b: That sounds great! Sunday 2 PM at Forth. I'll check with Priya and confirm back on this thread."])
    reasons = soc._not_agreed(fs, "Priya", plan_when="2026-09-20T14:00", plan_where="Forth")
    assert reasons and "checking with their person" in reasons[0]


def test_a_correction_is_not_a_stale_note(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t53c")
    monkeypatch.setattr(soc, "_network_index", lambda: {"p041": "Rielle"})
    note = soc.push_prompt(fs, "note", "I caught an error: Rielle's agent says they didn't agree to this. The question you got shouldn't have been asked.", log=False)
    soc.push_prompt(fs, "question", "I made an error about Rielle. Should I cancel this?", ["Cancel it", "Keep it"], log=False)
    assert next(q for q in soc.load_prompts(fs) if q["id"] == note["id"])["answer"] is None


def test_a_held_plan_is_cancelled_when_the_guests_agent_says_there_is_no_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK", str(tmp_path))
    import app
    import cahoots.social as soc
    from cahoots.agentfs import AgentFS

    fs = AgentFS("u-t53d")
    monkeypatch.setattr(soc, "_pids_for_name", lambda fs_, name: ["p041"])
    (fs.root / "calendar.json").write_text(json.dumps([{"when": "2026-09-21T19:30", "where": "Assiniboine Park", "who": "Rielle", "status": "held", "booked": "2026-09-14T11:09:00"}]))
    _thread(fs, "t-4950fb", ["[2026-09-14 11:10] p041 -> u-t53d: I think there's been a mix-up. We're on the same page that this isn't a match. No plan to confirm here."])
    assert app._cancel_denied_holds(fs) == 1
    assert json.loads((fs.root / "calendar.json").read_text())[0]["status"] == "cancelled"
    assert any("taken it off your calendar" in q["text"] for q in soc.load_prompts(fs))


def test_the_provider_is_logged_from_the_model_that_served_the_wake():
    import cahoots.social as soc

    class M:  # not an AnthropicModel
        pass
    assert soc._provider_of(type("A", (), {"model": M()})()) == "bedrock"
    from strands.models.anthropic import AnthropicModel

    fake = AnthropicModel.__new__(AnthropicModel)
    assert soc._provider_of(type("A", (), {"model": fake})()) == "anthropic"
