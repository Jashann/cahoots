"""Rails from the codebase audit: a courtesy is not a message, a conditional is not a yes, a guest joins a plan only through
the calendar, two places are compared by name not by two words, machinery never reaches the screen, and the idle line never
contradicts the plan beneath it."""
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
    import cahoots.social as social
    monkeypatch.setattr(social, "_NAMES_CACHE", {"t": 0, "index": {}})
    return tmp_path


def _card(pid="u-sam", name="Sam", days=(("sat", "18:00", "23:00"), ("sun", "12:00", "20:00"))):
    return Card(person_id=pid, first_name=name, one_liner=f"{name}, the literary fiction reader", interests=["literary fiction"], likes=["quiet"],
                modes=["friends"], neighbourhood="Wolseley", budget_cad=35, availability=[Availability(day=d, start=a, end=b) for d, a, b in days])


def _tools(social):
    return {t.tool_name: t for t in social.tools()}


def test_a_courtesy_is_not_a_message():
    from cahoots.social import _courtesy_only

    assert _courtesy_only("Thanks so much, I appreciate you circling back. All good!")
    assert _courtesy_only("Understood, thanks Marnie.")
    assert not _courtesy_only("Thanks! Would Saturday at 19:00 work instead?")
    assert not _courtesy_only("Thanks, but Sam can't do Thursday.")
    assert not _courtesy_only("Great, see you Saturday at 2 PM at Forth.")


def test_a_courtesy_on_an_existing_thread_is_not_sent(home, monkeypatch):
    from cahoots import wire
    from cahoots.social import Social, onboard

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Miriam", "- writes", _card("p001", "Miriam"))
    monkeypatch.setattr(wire, "REGISTRY", {"p001": "http://x/agents/p001"})
    s = Social("u-sam"); T = _tools(s)
    s.fs.write("threads/t-abc123.md", "[2026-09-13 11:00] Sam (me) -> p001: Saturday at 19:00 at Forth?\n[2026-09-13 11:01] p001 -> u-sam: Sorry, Miriam can't make that.\n")
    out = T["dm"](["p001"], "Thanks for letting me know, I appreciate it!", "t-abc123")
    assert out.startswith("not sent") and "wakes nobody" in out


def test_a_conditional_reply_is_not_a_yes(home):
    from cahoots.social import Social, _not_agreed, onboard

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Miriam", "- writes", _card("p001", "Miriam"))
    s = Social("u-sam")
    s.fs.write("people/p001.md", "# Miriam (p001)\n")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-1.md", f"[{now}] p001 -> u-sam: That sounds lovely! If it's the same, Wes would be very welcome to join.\n")
    out = _not_agreed(s.fs, "Miriam", plan_when="2026-09-19T14:00", plan_where="Forth")
    assert out and "conditional" in out[0]
    s.fs.write("threads/t-1.md", f"[{now}] p001 -> u-sam: Saturday 14:00 at Forth works, Miriam is in.\n")
    assert _not_agreed(s.fs, "Miriam", plan_when="2026-09-19T14:00", plan_where="Forth") == []


def test_a_guest_joins_a_plan_only_through_the_calendar_and_only_after_a_yes(home):
    from cahoots.social import Social, onboard

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Dorothy", "- reads", _card("p001", "Dorothy"))
    onboard("p003", "Olga", "- games", _card("p003", "Olga"))
    s = Social("u-sam"); T = _tools(s)
    s.fs.write("people/p001.md", "# Dorothy (p001)\n"); s.fs.write("people/p003.md", "# Olga (p003)\n")
    s.fs.write("calendar.json", json.dumps([{"when": "2026-09-20T14:00", "where": "Across the Board Game Café", "who": "Dorothy", "note": "", "status": "held"}]))
    assert "not added" in T["calendar"]("add", when="2026-09-20T14:00", who="Olga")  # Olga never said yes
    out = T["notify_human"]("Olga is joining the board games plan at 2 PM.")
    assert "not sent" in out and "calendar('add'" in out
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-2.md", f"[{now}] p003 -> u-sam: Saturday September 20 at 2 PM at Across the Board works, Olga is in.\n")
    assert "not added" in T["calendar"]("add", when="2026-09-20T14:00", who="Olga")  # her agent said yes, but Sam has not been asked
    from cahoots.social import answer_prompt, push_prompt
    q = push_prompt(s.fs, "question", "May Olga join the board games at Across the Board on Saturday at 2 PM?", ["Yes", "No"])
    answer_prompt(s.fs, q["id"], "Yes")
    assert T["calendar"]("add", when="2026-09-20T14:00", who="Olga", approved_by=q["id"]).startswith("added")  # both yeses: the guest joins
    assert "Olga" in json.loads(s.fs.read("calendar.json"))[0]["who"]
    assert T["notify_human"]("Olga is joining the board games plan at 2 PM.") == "sent"


def test_two_places_are_compared_by_name_not_by_two_words():
    from cahoots.social import _same_venue, _words_name_plan

    assert _same_venue("Thom Bargen", "Thom Bargen Coffee (Wolseley)")
    assert not _same_venue("Assiniboine Park (Leo Mol Sculpture Garden)", "Assiniboine Park Bike Loop")
    assert _same_venue("Assiniboine Park Bike Loop", "Assiniboine Park Bike Loop")
    t = datetime(2026, 9, 19, 14, 0)
    assert not _words_name_plan("Saturday at Forth works", t, "Across the Board Game Café")  # a different place on the list
    assert _words_name_plan("Saturday at Across the Board works", t, "Across the Board Game Café")


def test_machinery_never_reaches_the_screen_and_the_idle_line_never_contradicts_the_plan(home):
    from cahoots.social import Social, onboard, push_prompt
    from web.app import _HARNESS_WORDS, _mechanical_closer

    assert _HARNESS_WORDS.search("Something's wrong with the calendar system - let me know if this is an error I need to work around")
    assert _HARNESS_WORDS.search("the message got rewritten by the guard")
    assert not _HARNESS_WORDS.search("I've hit my message limit on that thread for today; I can ask tomorrow.")
    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam")
    import time
    s.fs.write("calendar.json", json.dumps([{"when": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT19:00"), "where": "Forth", "who": "Adrian", "status": "held"}]))
    line = _mechanical_closer(s.fs, time.time())
    assert "Forth" in line and "Nothing to decide" not in line
    push_prompt(s.fs, "question", "Sat 19:00 at Forth with Adrian. In?", ["In", "Not this time"])
    assert "question above" in _mechanical_closer(s.fs, time.time())


def test_a_yes_with_a_trailing_question_is_still_held_before_a_tap(home, monkeypatch):
    from cahoots import wire
    from cahoots.social import Social, onboard

    onboard("u-rowan", "Rowan", "- rides", _card("u-rowan", "Rowan"))
    s = Social("u-rowan")
    held = wire._hold_pre_tap_yes(s, "Saturday 2 PM works well for Rowan - how about Sargent Sundae instead?")
    assert "works well for Rowan" not in held and "I'll check with Rowan" in held and "how about Sargent Sundae instead?" in held
    assert wire._hold_pre_tap_yes(s, "Rowan can make Saturday at 2, go ahead and book it.").startswith("I'll check with Rowan")
    assert wire._hold_pre_tap_yes(s, "Does Saturday at 2 work for your person?") == "Does Saturday at 2 work for your person?"  # a question is a question


def test_a_time_before_the_weekday_is_caught_and_after_dark_allows_the_afternoon(home):
    from cahoots.social import Social, _named_slot_clash, _named_venue_clash, _when_in_text, onboard

    c = _card()  # free sat 18:00-23:00, sun 12:00-20:00
    from cahoots.social import _named_time_clash
    assert _named_time_clash(c, "How about 10 PM on Tuesday?") is not None  # a day with no window at all is the weekday rail's
    assert _named_slot_clash(c, "How about 10 PM on Sunday?") is not None  # a day with a window: the slot rail, time first
    assert _named_slot_clash(c, "22:00 on Sunday?") is not None
    assert _named_slot_clash(c, "2 PM on Sunday works") is None
    assert _when_in_text("Saturday at 2 PM at Forth")[11:16] == "14:00"
    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam")
    s.fs.write("me.md", "# Sam\n- avoids Sherbrook Street after dark\n")
    assert _named_venue_clash(s.fs, "Coffee at Thom Bargen on Saturday at 2 PM?") is None  # Thom Bargen is on Sherbrook; daytime is fine
    assert _named_venue_clash(s.fs, "Drinks at Thom Bargen on Saturday at 9 PM?") is not None


def test_a_behaviour_line_is_never_about_a_person(home, monkeypatch):
    from cahoots.social import Social, onboard
    import json as _json
    from datetime import datetime, timedelta

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Miriam", "- writes", _card("p001", "Miriam"))
    s = Social("u-sam"); T = _tools(s)
    when = (datetime.now() - timedelta(hours=3)).isoformat(timespec="minutes")
    s.fs.write("calendar.json", _json.dumps([{"when": when, "where": "Thom Bargen", "who": "Miriam (p001)", "status": "confirmed"}]))
    s.fs.write("people/p001.md", "# Miriam (p001)\n")
    s._payload = "Q: How was it?\nA: fine"; s.reason = "answer"
    assert "not sent" in T["pass_behaviour"]("p001", "someone who listens more next time")
    assert "not sent" in T["pass_behaviour"]("p001", "keep it to two people next time")
    assert "not sent" in T["pass_behaviour"]("p001", "less loud next time")
    assert "not sent" in T["pass_behaviour"]("p001", "she should interrupt less next time")


def test_only_tell_the_agents_keeps_the_ask_sayable_and_off_the_card(home):
    from cahoots.social import Social, keep_ask_private, load_card, onboard, pending_asks, record_ask

    onboard("u-sam", "Sam", "- reads", _card())
    s = Social("u-sam")
    assert record_ask(s.fs, "I'm starting a clothing label, find me a manufacturer in town")
    assert pending_asks(s.fs) and not load_card(s.fs).asks
    assert keep_ask_private(s.fs, "I'm starting a clothing label, find me a manufacturer in town")
    assert pending_asks(s.fs), "the ask must stay sayable to the agents it writes to"
    assert not load_card(s.fs).asks, "and stay off the public card"


def test_a_mid_thread_objection_is_owed_and_a_third_party_yes_is_scrubbed(home, monkeypatch):
    from cahoots import wire
    from cahoots.social import Social, onboard
    from web.app import _queue_owed_replies
    import time

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p001", "Dorothy", "- reads", _card("p001", "Dorothy"))
    onboard("p003", "Marcus", "- games", _card("p003", "Marcus"))
    s = Social("u-sam")
    t0 = time.time() - 1
    now = "2026-09-13 18:40"
    s.fs.write("threads/t-grp001.md", f"[{now}] Sam (me) -> p001, p003: Saturday 14:00 at Across the Board, both in?\n"
                                      f"[{now}] p001 -> u-sam, p003: Could we move it to 15:00? Dorothy can't make 14:00.\n"
                                      f"[{now}] p003 -> u-sam, p001: Marcus is in for 14:00, see you there.\n")
    assert _queue_owed_replies("u-sam", t0) == 1  # Dorothy's move request in the middle of the thread is owed, even though Marcus spoke last
    assert wire._ABOUT.search("Priya's in for Saturday") and wire._ABOUT.search("Priya is in")


def test_a_courtesy_is_not_a_yes_and_a_declined_guest_leaves_the_plan(home):
    from cahoots.social import Social, _not_agreed, onboard
    from web.app import _drop_declined_guests
    import json as _json

    onboard("u-sam", "Sam", "- reads", _card())
    onboard("p003", "Olga", "- games", _card("p003", "Olga"))
    onboard("p020", "Harold", "- books", _card("p020", "Harold"))
    s = Social("u-sam")
    s.fs.write("people/p003.md", "# Olga (p003)\n"); s.fs.write("people/p020.md", "# Harold (p020)\n")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-c1.md", f"[{now}] p003 -> u-sam: Looking forward to it then!\n")
    assert any("courtesy" in r for r in _not_agreed(s.fs, "Olga", plan_when="2026-09-19T14:00", plan_where="Across the Board"))
    when = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT14:00")
    s.fs.write("calendar.json", _json.dumps([{"when": when, "where": "Across the Board Game Café", "who": "Harold, Olga", "status": "held", "booked": datetime.now().isoformat(timespec="seconds")}]))
    later = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-c2.md", f"[{later}] p003 -> u-sam: Sorry, Olga can't make Saturday after all.\n")  # her last word, after the courtesy
    assert _drop_declined_guests(s.fs) == 1
    assert _json.loads(s.fs.read("calendar.json"))[0]["who"] == "Harold"


def test_two_real_peoples_agents_never_wait_on_each_others_tap(home):
    from cahoots.social import Social, load_prompts, onboard

    onboard("u-kit", "Kit", "- nights", _card("u-kit", "Kit", days=(("thu", "14:00", "16:00"),)))
    onboard("u-wes", "Wes", "- prints", _card("u-wes", "Wes", days=(("thu", "12:00", "18:00"),)))
    s = Social("u-kit"); T = _tools(s)
    s.fs.write("people/u-wes.md", "# Wes (u-wes)\n")
    thu = datetime.now() + timedelta(days=(3 - datetime.now().weekday()) % 7 or 7)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s.fs.write("threads/t-w1.md", f"[{now}] Kit (me) -> u-wes: Thursday 14:30 at Thom Bargen Coffee?\n[{now}] u-wes -> u-kit: I'll check with Wes and confirm once I know Kit is in. Is Kit available?\n")
    out = T["ask_human"](f"{thu.strftime('%A %d %B')}, 14:30 at Thom Bargen Coffee with Wes: coffee. In?", ["In", "Not this time"],
                         plan_when=thu.strftime("%Y-%m-%dT14:30"), plan_where="Thom Bargen Coffee", plan_who="Wes")
    assert out.startswith("asked ("), out  # Wes's agent is checking on its side: Kit is asked, not blocked
    q = load_prompts(s.fs)[-1]
    assert "still checking on their side" in q["text"] and "held until Wes confirms" in q["plan"]["note"]


def test_replay_cuts_on_a_clause_and_names_venues():
    from web.app import _cut, _humanise

    assert _cut("Saturday 14:00 is outside Kit's free hours (thu 14:00-16:00, sat 10:00-12:00)", 60).endswith("…")
    assert "(" not in _cut("free windows (sat 18:00-23:00, sun 12:00-20:00) and more words here", 40).rstrip("…") or _cut("free windows (sat 18:00-23:00, sun 12:00-20:00) and more words here", 40).count("(") == _cut("free windows (sat 18:00-23:00, sun 12:00-20:00) and more words here", 40).count(")")
    assert "thom-bargen" not in _humanise("thom-bargen: Thom Bargen Coffee (Wolseley)")


def test_the_shortlist_prefers_people_free_at_the_same_time(home):
    from cahoots.directory import Directory, windows_overlap
    from cahoots.social import onboard

    onboard("u-kit", "Kit", "- nights", _card("u-kit", "Kit", days=(("sat", "10:00", "12:00"),)))
    for pid, name, days in [("u-a1", "Ann", (("sat", "09:00", "13:00"),)), ("u-b2", "Ben", (("wed", "18:00", "22:00"),)), ("u-c3", "Cal", (("sat", "11:00", "15:00"),)), ("u-d4", "Dee", (("sat", "08:00", "11:00"),))]:
        onboard(pid, name, "- reads literary fiction", _card(pid, name, days=days))
    d = Directory.load()
    mine = [{"day": "sat", "start": "10:00", "end": "12:00"}]
    hits = d.search("literary fiction reader", exclude="u-kit", overlap_with=mine)
    ids = [e["person_id"] for e in hits]
    assert "u-b2" not in ids and set(ids) >= {"u-a1", "u-c3", "u-d4"}  # three overlap, so the one who is never free when Kit is drops out
    assert "FREE TOGETHER sat 10:00-12:00" in d.describe(hits[[e["person_id"] for e in hits].index("u-a1")])
    assert windows_overlap(mine, [{"day": "sat", "start": "11:00", "end": "15:00"}]) == "sat 11:00-12:00"
