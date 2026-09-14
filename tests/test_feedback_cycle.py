"""After an outing: the question is asked mechanically, the person's words stay in the folder, only behaviour for next time
leaves (through its own rails), a tap books the plan it was shown whatever the notes say, and a guest's no takes the
question back."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("CAHOOTS_GUARDRAIL_OFF", "1")
os.environ.setdefault("CAHOOTS_NO_LOOP", "1")

from cahoots.schemas import Availability, Card


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK_DIR", str(tmp_path))
    return tmp_path


def _card(pid="u-sam", name="Sam"):
    return Card(person_id=pid, first_name=name, one_liner=f"{name}, the literary fiction reader", interests=["literary fiction", "board games"],
                likes=["quiet"], modes=["friends"], neighbourhood="Wolseley", budget_cad=35,
                availability=[Availability(day="sat", start="18:00", end="23:00"), Availability(day="sun", start="12:00", end="20:00")])


def _tools(social):
    return {t.tool_name: t for t in social.tools()}


def _past_outing(s, hours_ago=3, who="Miriam (p001), Simone (p002)", where="Thom Bargen, Wolseley"):
    when = (datetime.now() - timedelta(hours=hours_ago)).replace(second=0, microsecond=0).isoformat(timespec="minutes")
    s.fs.write("calendar.json", json.dumps([{"when": when, "where": where, "who": who, "note": "", "booked": when, "status": "confirmed"}]))
    s.fs.write("people/p001.md", "# Miriam (p001)\n- novelist\n")
    s.fs.write("people/p002.md", "# Simone (p002)\n- illustrator\n")
    return when


def test_the_after_outing_question_is_mechanical_and_the_words_stay_in_the_folder(home):
    from cahoots.social import Social, load_prompts, onboard
    from web.app import _after_outing, _record_feedback

    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam")
    when = _past_outing(s)
    _after_outing("u-sam", {"when": when, "where": "Thom Bargen, Wolseley", "who": "Miriam (p001), Simone (p002)"})  # no model, no budget
    qs = [q for q in load_prompts(s.fs) if q.get("kind") == "feedback"]
    assert len(qs) == 1 and qs[0]["options"] == ["Great", "Fine", "Not for me"] and qs[0]["text"] == "How was Thom Bargen, Wolseley with Miriam, Simone?"
    assert qs[0]["outing"]["where"] == "Thom Bargen, Wolseley" and "p001" in qs[0]["outing"]["who"]
    touched = _record_feedback(s.fs, qs[0], "Not bad. Miriam talked over everyone; Simone was great, more of that.")
    assert "people/p001.md" in touched and "people/p002.md" in touched and any(t.startswith("outings/") for t in touched)
    assert "talked over everyone" in s.fs.read("people/p001.md") and "private" in s.fs.read("people/p001.md")
    assert "talked over everyone" in s.fs.read([t for t in touched if t.startswith("outings/")][0])


def test_how_was_it_keeps_its_own_buttons_and_is_never_a_plan_question(home):
    from cahoots.social import Social, load_prompts, onboard

    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam"); _past_outing(s); T = _tools(s)
    out = T["ask_human"]("How was coffee with Miriam and Simone at Thom Bargen?", ["Great", "Fine", "Not for me"])
    assert out.startswith("asked (")
    q = [q for q in load_prompts(s.fs) if q.get("kind") == "feedback"][0]
    assert q["options"] == ["Great", "Fine", "Not for me"] and q["outing"]["where"] == "Thom Bargen, Wolseley"
    assert "already asked" in T["ask_human"]("How was coffee with Miriam and Simone at Thom Bargen?", ["Great", "Fine"])


def test_behaviour_for_next_time_leaves_through_its_own_rails_and_the_quote_never_does(home, monkeypatch):
    from cahoots import wire
    from cahoots.social import Social, onboard

    import cahoots.social as social

    monkeypatch.setattr(social, "_NAMES_CACHE", {"t": 0, "index": {}})
    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Miriam", "- writes", _card("p001", "Miriam"))
    onboard("p003", "Olga", "- games", _card("p003", "Olga"))
    s = Social("u-sam"); _past_outing(s); T = _tools(s)
    s._payload = "Q: How was Thom Bargen with Miriam, Simone?\nA: Not bad. Miriam talked over everyone; Simone was great, more of that."
    s.reason = "answer"
    sent = {}

    async def fake_send(url, msg):
        sent[url] = msg
        return "noted, thank you"
    monkeypatch.setattr(wire, "_send_one", fake_send)
    monkeypatch.setattr(wire, "REGISTRY", {"p001": "http://x/agents/p001", "p002": "http://x/agents/p002", "p003": "http://x/agents/p003"})
    assert "not on a plan that has happened" in T["pass_behaviour"]("p003", "a bit more room to talk next time")
    assert "quote" in T["pass_behaviour"]("p001", "Miriam talked over everyone next time") or "names" in T["pass_behaviour"]("p001", "Miriam talked over everyone next time")
    assert "not sent" in T["pass_behaviour"]("p001", "last time was a bit much, less next time")  # META
    assert "not sent" in T["pass_behaviour"]("p001", "my person felt ignored, more room next time")  # who said it
    assert "not sent" in T["pass_behaviour"]("p001", "coffee")  # nothing about next time
    out = T["pass_behaviour"]("p001", "a bit more room to talk next time")
    assert out.startswith("thread t-"), out
    env = wire.parse_envelope(sent["http://x/agents/p001"])
    assert env["text"] == "For next time: a bit more room to talk next time."
    thread = s.fs.read("threads/" + out.split()[1] + ".md")
    assert "[guard: behaviour]" in thread and "talked over" not in thread


def test_a_tap_books_the_plan_it_was_shown_whatever_the_notes_say(home):
    from cahoots.social import Social, _standing_commitments, answer_prompt, book_from_question, onboard, push_prompt

    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam")
    sat = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7 or 7)
    when = sat.strftime("%Y-%m-%dT19:00")
    s.fs.write("me.md", f"# Sam\n- Saturday 7 PM board games with Trevor at Across the Board (proposed, q-abc123)\n- choir every Tuesday at 7pm\n- meeting Trevor Saturday 7 pm for the game night plan\n")
    found = _standing_commitments(s.fs)
    assert [w for w in found if w[0] == 1] and not [w for w in found if w[0] == 5], found  # choir is a commitment; this week's plan is not
    q = push_prompt(s.fs, "question", f"{sat.strftime('%a %d %b')}, 19:00 at Across the Board Game Café with Trevor. In?", ["In", "Not this time"],
                    plan={"when": when, "where": "Across the Board Game Café", "who": "Trevor", "note": ""})
    answer_prompt(s.fs, q["id"], "In")
    out = book_from_question(s, q)
    assert out.startswith("booked"), out
    cal = json.loads(s.fs.read("calendar.json"))
    assert cal[0]["when"] == when and cal[0]["status"] == "held"


def test_a_yes_by_another_name_is_the_same_button(home):
    from cahoots.social import Social, load_prompts, onboard

    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam"); T = _tools(s)
    sat = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7 or 7)
    out = T["ask_human"](f"{sat.strftime('%A %d %B')} at 19:00 at Across the Board Game Café with Zed: board games. In?", ["Yes", "No"],
                         plan_when=sat.strftime("%Y-%m-%dT19:00"), plan_where="Across the Board Game Café", plan_who="Zed")
    assert out.startswith("asked ("), out
    assert load_prompts(s.fs)[-1]["options"] == ["In", "Not this time"]


def test_a_guests_no_takes_the_question_back_with_a_note(home, monkeypatch):
    from cahoots.social import Social, load_prompts, onboard, push_prompt
    from web.app import _withdraw_declined

    import cahoots.social as social

    monkeypatch.setattr(social, "_NAMES_CACHE", {"t": 0, "index": {}})  # the directory index is cached a minute; this test has its own network
    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p003", "Olga", "- games", _card("p003", "Olga"))
    s = Social("u-sam")
    s.fs.write("people/p003.md", "# Olga (p003)\n- board games\n")
    tue = datetime.now() + timedelta(days=(1 - datetime.now().weekday()) % 7 or 7)
    q = push_prompt(s.fs, "question", "Olga is in: Tuesday 18:30 at Across the Board Game Café. In?", ["In", "Not this time"],
                    plan={"when": tue.strftime("%Y-%m-%dT18:30"), "where": "Across the Board Game Café", "who": "Olga", "note": ""})
    assert _withdraw_declined(s.fs) == 0
    later = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-aaa111.md", f"[{later}] p003 -> u-sam: Olga can't make Tuesday after all, sorry; she prefers bigger groups.\n")
    assert _withdraw_declined(s.fs) == 1
    ps = load_prompts(s.fs)
    assert next(x for x in ps if x["id"] == q["id"])["answer"] == "withdrawn"
    assert any(x["kind"] == "note" and "taken back" in x["text"] and "Olga" in x["text"] for x in ps)


def test_the_wire_names_a_day_that_exists_and_a_decline_may_name_the_day():
    from cahoots.social import _day_date_mismatch, _named_time_clash

    assert _day_date_mismatch("Saturday September 20th at 10am works") is not None  # 2026-09-20 is a Sunday
    assert _day_date_mismatch("Sunday September 20th at 10am") is None
    assert _day_date_mismatch("Saturday 19 September, 10:00") is None
    assert _day_date_mismatch("Saturday (2026-09-20) at 10") is not None
    card = _card()
    assert _named_time_clash(card, "Thursday doesn't work for Sam; Saturday evening instead?") is None
    assert _named_time_clash(card, "Thursday at 7 works for Sam") is not None


def test_public_card_fields_get_the_floor_and_never_say_new_in_town():
    from cahoots.cards import scrub_public_fields

    c = _card()
    c.can_help_with = "medications and pediatric health questions; bike repair, zine layout"
    c.one_liner = "Sam, new to Winnipeg and still meeting people, the reader"
    c.notes_for_agents = "Just moved here from Halifax. Likes long walks."
    scrub_public_fields(c)
    assert c.can_help_with == "bike repair; zine layout"
    assert "new to" not in c.one_liner.lower() and "reader" in c.one_liner
    assert "moved" not in c.notes_for_agents.lower() and "long walks" in c.notes_for_agents


def test_a_bedrock_refusal_is_remembered_across_a_restart(home, monkeypatch):
    from cahoots import person_agent as pa

    monkeypatch.setattr(pa, "_DENIED_AT", 0.0)
    assert not pa.recently_denied()
    pa.mark_denied()
    monkeypatch.setattr(pa, "_DENIED_AT", 0.0)  # a fresh process
    assert pa.recently_denied()


def test_a_card_is_never_shown_unless_it_comes_from_this_persons_memory():
    from cahoots.cards import card_grounded
    from cahoots.schemas import Card

    memory = ("Rowan is 38, a bike mechanic at a co-op in Wolseley. Loves bike touring, fixing things, punk shows, "
              "second-hand bookshops and cooking. Wants to meet quiet creative people.")
    good = Card(person_id="u-rowan", first_name="Rowan", one_liner="Rowan, the bike mechanic who can fix anything",
                interests=["bike touring", "fixing things", "second-hand bookshops", "punk shows", "cooking"],
                likes=["quiet"], modes=["friends"], neighbourhood="Wolseley", budget_cad=25,
                can_help_with="fixing bikes and anything mechanical",
                availability=[Availability(day="sat", start="12:00", end="23:00")])
    crossed = Card(person_id="u-rowan", first_name="Rowan", one_liner="Wes, the letterpress printer and choir singer",
                   interests=["board games", "birding along the Assiniboine", "secondhand bookshops", "letterpress printing", "community choir"],
                   likes=["quiet"], modes=["friends"], neighbourhood="Wolseley", budget_cad=25,
                   can_help_with="learning letterpress printing; can help someone set type",
                   availability=[Availability(day="sat", start="12:00", end="23:00")])
    assert card_grounded(good, memory)
    assert not card_grounded(crossed, memory)  # another person's card verbatim: refused
    assert card_grounded(crossed, "")  # nothing to judge against: not refused (the human still approves)


def test_behaviour_refuses_a_name_it_has_not_indexed(home, monkeypatch):
    from cahoots.social import Social, onboard

    import cahoots.social as social
    monkeypatch.setattr(social, "_NAMES_CACHE", {"t": 0, "index": {}})
    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Miriam", "- writes", _card("p001", "Miriam"))
    s = Social("u-sam"); _past_outing(s); T = _tools(s)
    s._payload = "Q: How was it?\nA: fine"
    s.reason = "answer"
    # "Delphine" is a real name the network never indexed: a plain in-list check would miss it; the proper-noun rail catches it
    assert "not sent" in T["pass_behaviour"]("p001", "more like the night at Delphine's next time")
    assert "not sent" in T["pass_behaviour"]("p001", "somewhere quieter than The Handsome Daughter next time")


def test_a_weekday_slot_in_24_hour_time_is_caught():
    from cahoots.social import _named_slot_clash

    c = _card()  # free sat 18:00-23:00, sun 12:00-20:00
    assert _named_slot_clash(c, "How about Sunday at 22:00 at Forth?") is not None  # 22:00 is past Sam's Sunday window
    assert _named_slot_clash(c, "Sunday at 15:00 works") is None
    assert _named_slot_clash(c, "Saturday 19:00 at Across the Board") is None


def test_a_note_may_not_claim_a_guest_is_joining_before_they_agreed(home, monkeypatch):
    import cahoots.social as social
    monkeypatch.setattr(social, "_NAMES_CACHE", {"t": 0, "index": {}})
    from cahoots.social import Social, onboard
    import json as _json

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p003", "Olga", "- games", _card("p003", "Olga"))
    onboard("p001", "Dorothy", "- reads", _card("p001", "Dorothy"))
    s = Social("u-sam"); T = _tools(s)
    s.fs.write("calendar.json", _json.dumps([{"when": "2026-09-20T14:00", "where": "Across the Board Game Café", "who": "Dorothy", "note": "", "status": "held"}]))
    # Olga never said yes and is not on the calendar: a note claiming she's joining is refused
    out = T["notify_human"]("Good news: Olga is joining the board games plan, so it's you, Dorothy and Olga at 2 PM.")
    assert "not sent" in out and "Olga" in out
    # a note about only who is actually on the plan is fine
    assert T["notify_human"]("Dorothy is set for the board games plan at 2 PM.") == "sent"
    # honest waiting language is not blocked
    assert T["notify_human"]("Dorothy is in; still waiting to hear back from Olga.") == "sent"
