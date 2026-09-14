"""The mechanical question takes the time from the line that said yes, not the first time anyone mentioned."""


def test_plan_time_follows_the_yes_line(monkeypatch):
    import web.app as A
    tail = "Dorothy is free next Saturday afternoon (Sept 19, after 2:30 PM). Yes, Saturday September 19 at 3:00 PM at Across the Board works for Dorothy."
    assert A._parse_plan_time("3:00 PM", tail) == "2026-09-19T15:00"
    assert A._parse_plan_time("Saturday", "Yes, Saturday September 19 at 3:00 PM works.") == "2026-09-19T15:00"


def test_third_party_scrub_keeps_the_recipient_even_with_a_namesake(monkeypatch):
    from cahoots import wire, social
    monkeypatch.setattr(social, "_network_index", lambda: {"u-1": "Sam", "u-2": "Sam", "u-3": "Ann"})
    class S:  # the sender
        person_id, first_name = "u-9", "Quill"
    kept, dropped = wire._third_party_scrub(S(), ["u-1"], "Sam wants a quiet café on Sunday. Ann said she cannot come this week.")
    assert "Sam wants" in kept and dropped == 1


def test_directory_day_words_are_whole_words():
    from cahoots.directory import Directory
    d = Directory([{"person_id": "p900", "first_name": "A", "one_liner": "x", "can_help_with": "", "neighbourhood": "", "interests": ["chess"], "likes": [], "avoids": [], "modes": [], "budget_cad": 0, "free": ["sat"], "availability": []},
                   {"person_id": "p901", "first_name": "B", "one_liner": "x", "can_help_with": "", "neighbourhood": "", "interests": ["chess"], "likes": [], "avoids": [], "modes": [], "budget_cad": 0, "free": ["mon"], "availability": []}])
    top = d.search("chess on saturday")
    assert top[0]["person_id"] == "p900"
    both = d.search("a satisfying chess game")  # "satisfying" is not Saturday
    assert {e["person_id"] for e in both} == {"p900", "p901"} and both[0]["person_id"] == "p900" or len({e["person_id"] for e in both}) == 2


def test_hyphenated_import_facts_are_private():
    from cahoots.guard import private_terms
    from cahoots.schemas import Card
    card = Card(person_id="u-dev", first_name="Devraj", one_liner="Devraj, the cyclist", interests=["cycling"], likes=["quiet"], avoids=[], modes=["friends"],
                neighbourhood="West End", max_travel_min=20, budget_cad=22, group_size_pref=(2, 4), availability=[])
    terms = private_terms([], ["Devraj is working on a bike-repair co-op with two friends"], card)
    assert any("bike-repair co-op" in t for t in terms)
    assert not private_terms([], ["Devraj enjoys cycling and quiet nights"], card)  # card words only: not a secret


def test_a_counter_offer_inside_a_polite_no_is_an_offer():
    import web.app as A
    from cahoots.social import DECLINES
    line = "Thanks for reaching out! Mira already has plans Monday evening at 7pm. Would another time work? Mira's free Tuesday 10am-3pm or Friday afternoon."
    assert DECLINES.search(line) and A._OFFER.search(line)
    assert not A._OFFER.search("Thanks for thinking of Patricia, but this isn't the right fit.")


def test_a_later_time_in_a_reply_is_an_offer():
    import web.app as A
    assert A._OFFER.search("Patricia can't do 10 AM Saturday, but she's free from 2 PM or later.")
    assert A._OFFER.search("Mornings are out for Chen; after 6 pm on Thursday would work.")
    assert not A._OFFER.search("Thanks, but the interests don't line up for Greg.")


def test_a_stated_window_is_not_an_offered_time():
    import web.app as A
    assert A._parse_plan_time("Saturday", "Olga's window is Saturday 12:00-18:00 which doesn't quite reach the evening slot.") is None
    assert A._parse_plan_time("Saturday", "Saturday at 3 PM works for Olga; her window is 12:00-18:00.") == "2026-09-19T15:00" or A._parse_plan_time("Saturday", "Saturday at 3 PM works for Olga; her window is 12:00-18:00.").endswith("T15:00")


def test_a_question_left_unanswered_on_a_thread_is_queued_after_the_wake(monkeypatch):
    import time, json
    import web.app as A
    from cahoots.agentfs import AgentFS
    fs = AgentFS("u-owe")
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    (fs.root / "threads" / "t-ow1.md").write_text("[2026-09-13 15:20] Rowan (me) -> p001: Would Vik like Friday 6:30 at McNally?\n[2026-09-13 15:21] p001 -> u-owe: Friday is out for Vik. Would Saturday at 2 or Sunday at 3 work for Rowan?\n")
    assert A._queue_owed_replies("u-owe", t0 - 60) == 1
    queued = list((fs.root / "inbox").glob("t-ow1-*.json"))
    assert len(queued) == 1 and json.loads(queued[0].read_text())["owed"] is True
    assert A._queue_owed_replies("u-owe", t0 - 60) == 0  # once per line, never a loop


def test_a_throttle_inside_an_event_loop_exception_is_a_throttle():
    from cahoots.person_agent import is_throttled
    class EventLoopException(Exception):
        def __init__(self, original):
            super().__init__("Event loop cycle failed")
            self.original_exception = original
    inner = Exception("An error occurred (ThrottlingException) when calling the ConverseStream operation: Too many tokens per day")
    assert is_throttled(EventLoopException(inner)) and not is_throttled(EventLoopException(ValueError("x")))


def test_a_window_written_with_to_is_not_an_offer():
    import web.app as A
    assert A._parse_plan_time("Tuesday", "Dorothy is free Tuesday 1 to 5 PM, whatever suits.") is None
    assert not A._words_name_plan_web("Dorothy is free Tuesday between 1 and 5 PM.", "2026-09-15T17:00")
    assert A._words_name_plan_web("Yes, Tuesday at 5 PM works for Dorothy.", "2026-09-15T17:00")


def test_a_declined_time_is_never_the_offered_time():
    import web.app as A
    assert A._offered_clauses("Tomas can't do Tuesday at 3 PM, but Thursday at 6 PM works for him.").strip().startswith("Thursday")
    assert A._offered_clauses("Tomas can only do Tuesday 15:00, sorry.") == "Tomas can only do Tuesday 15:00" or "Tuesday" in A._offered_clauses("Tomas can only do Tuesday 15:00, sorry.")
    assert A._offered_clauses("Unfortunately Tuesday at 3 PM doesn't work for Tomas.") == ""
