

def test_holding_reply_means_checking_not_yes(tmp_path, monkeypatch):
    """Dutch's agent proposed Tuesday at Forth, then said "I'll check with Dutch and come back": the plan may still be asked of a
    real person, but with the caveat that Dutch is still checking, never as Dutch's yes (a courtesy on the holding line
    had been read as a firm yes)."""
    from cahoots import social
    from cahoots.agentfs import AgentFS
    monkeypatch.setattr(social, "_network_index", lambda: {"u-63e403": "Dutch"})
    fs = AgentFS("u-12db72")
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    now = social.local_now()
    t1 = (now - social.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"); t2 = (now - social.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
    (fs.root / "threads" / "t-299d5a.md").write_text(
        f"[{t1}] u-63e403 -> u-12db72: Yes, Dutch would be interested in meeting Marnie. How about Tuesday evening, September 15th at 7 PM at Forth?\n"
        f"[{t2}] u-63e403 -> u-12db72: I'll check with Dutch and come back to you on this thread.\n")
    reasons = social._not_agreed(fs, "Dutch")
    assert len(reasons) == 1 and "checking with their person" in reasons[0] and "has not said yes yet" in reasons[0]  # the wording ask_human turns into "(Dutch is still checking on their side)"
    (fs.root / "threads" / "t-299d5a.md").write_text(f"[{t2}] u-63e403 -> u-12db72: I'll check with Dutch and come back to you on this thread.\n")
    assert "checking with their person" in social._not_agreed(fs, "Dutch")[0]  # a holding line alone is still a reply: they are checking, not silent


def test_bare_in_with_a_complete_plan_is_composed():
    from cahoots import social
    assert social._plan_time_words("2026-09-15T19:00") == "Tue 15 Sep, 19:00"
    assert social._plan_time_words("Tuesday evening") == "Tuesday evening"


def test_polite_decline_is_a_no_and_namesakes_resolve(tmp_path, monkeypatch):
    from cahoots import social
    from cahoots.agentfs import AgentFS
    monkeypatch.setattr(social, "_network_index", lambda: {"p001": "Vik", "u-aaaaaa": "Quill", "u-bbbbbb": "Quill"})
    fs = AgentFS("u-me")
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    now = social.local_now(); t1 = (now - social.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M")
    (fs.root / "threads" / "t-1.md").write_text(f"[{t1}] p001 -> u-me: Vik has something at 7 PM Monday, so the timing doesn't quite work, but the quiet, nerdy vibe sounds good.\n")
    assert social._not_agreed(fs, "Vik") and "no" in social._not_agreed(fs, "Vik")[0]
    # two Quills on the network: the one we talked to is the counterpart
    (fs.root / "threads" / "t-2.md").write_text(f"[{t1}] u-bbbbbb -> u-me: Yes, count me in for Saturday.\n")
    assert social._pids_for_name(fs, "Quill") == ["u-bbbbbb"]
    assert social._not_agreed(fs, "Quill") == []


def test_a_yes_to_another_time_is_not_a_yes_to_this_plan():
    from cahoots import social
    assert social._yes_to_another_time("Yes, Sunday September 20 at 2 PM works for Adrian.", "2026-09-20T15:00")
    assert not social._yes_to_another_time("Yes, Sunday September 20 at 3 PM works for Adrian.", "2026-09-20T15:00")
    assert not social._yes_to_another_time("Yes, count Adrian in.", "2026-09-20T15:00")  # a bare yes is a yes to the plan under discussion
    assert not social._yes_to_another_time("3:00 PM on the 20th is fine", "2026-09-20T15:00")


def test_avoided_place_with_a_qualifier_matches_the_street():
    from cahoots.social import _avoid_terms
    t = _avoid_terms("# Notes\n- Avoid: Sherbrook Street after dark, The Handsome Daughter\nAlso avoid Corydon Avenue on weekends.")
    assert "Sherbrook Street" in t and "Handsome Daughter" in t and any(x.startswith("Corydon") for x in t)


def test_an_invitation_phrased_as_a_question_is_not_a_yes(tmp_path, monkeypatch):
    from cahoots import social
    from cahoots.agentfs import AgentFS
    monkeypatch.setattr(social, "_network_index", lambda: {"p007": "Rowan"})
    fs = AgentFS("u-me2")
    (fs.root / "threads").mkdir(parents=True, exist_ok=True)
    t1 = (social.local_now() - social.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
    (fs.root / "threads" / "t-9.md").write_text(f"[{t1}] p007 -> u-me2: We'd love to have your person along too; would Saturday at 2 work for them?\n")
    assert social._not_agreed(fs, "Rowan")


def test_a_tap_books_only_the_plan_it_was_shown():
    from cahoots import social
    from cahoots.social import Social, push_prompt, answer_prompt, book_plan, onboard
    onboard("u-tap", "Tia", "- likes quiet cafes", social.Card(person_id="u-tap", first_name="Tia", one_liner="Tia", interests=["books"], likes=["quiet"], avoids=[], modes=["friends"],
            neighbourhood="Wolseley", max_travel_min=25, budget_cad=30, group_size_pref=(2, 4), availability=[{"day": "sat", "start": "12:00", "end": "18:00"}]))
    me = Social("u-tap")
    q = push_prompt(me.fs, "question", "Make an exception this once?", ["Yes, this once", "No"])
    answer_prompt(me.fs, q["id"], "Yes, this once")
    out = book_plan(me, "2026-09-19T18:30", "Across the Board Game Café", "Olga", approved_by=q["id"])
    assert out.startswith("not booked") and ("did not carry this plan" in out or "not a yes to a plan" in out)
    q2 = push_prompt(me.fs, "question", "Saturday 19 September, 14:00 at Thom Bargen with Olga. In?", ["In", "Not this time"], plan={"when": "2026-09-19T14:00", "where": "Thom Bargen Coffee", "who": "Olga", "note": ""})
    answer_prompt(me.fs, q2["id"], "In")
    assert book_plan(me, "2026-09-19T14:00", "Thom Bargen Coffee", "Olga", approved_by=q2["id"]).startswith("booked")


def test_a_yes_for_an_untapped_person_is_held_in_any_phrasing():
    from cahoots import wire, social
    from cahoots.agentfs import AgentFS
    class S:
        person_id, first_name, simulated = "u-rw", "Rowan", False
        fs = AgentFS("u-rw")
    out = wire._hold_pre_tap_yes(S(), "Yes! Rowan would love to join. Sunday 2 PM at Forth works perfectly. Should we make it three of us?")
    assert "would love to join" not in out and "I'll check with Rowan" in out and "three of us?" in out
    assert wire._hold_pre_tap_yes(S(), "I'll check with Rowan and come back to you. Is 2 PM firm?") == "I'll check with Rowan and come back to you. Is 2 PM firm?"


def test_step_free_is_a_derived_card_limit():
    from cahoots.cards import DERIVED
    assert DERIVED.search("Prefers step-free venues")


def test_an_ask_is_cut_at_a_word_or_sentence_boundary():
    from cahoots.social import _ask_words
    long = "I'm starting a small clothing label and I need a manufacturer in town who does small batches, ideally cut-and-sew, and can start next month with linen and organic cotton from a supplier who ships to Winnipeg."
    out = _ask_words(long)
    assert len(out) <= 160 and not out.endswith("Winnip") and out.split()[-1] in [w.strip(",.") for w in long.split()]
    assert _ask_words("find me a chess partner") == "find me a chess partner"


def test_place_words_are_not_private_names():
    from cahoots.guard import private_names
    from cahoots.schemas import Card
    card = Card(person_id="u-pn", first_name="Tobin", one_liner="Tobin", interests=["chess"], likes=[], avoids=[], modes=["friends"], neighbourhood="West End",
                max_travel_min=20, budget_cad=20, group_size_pref=(2, 4), availability=[])
    names = private_names(["- grew up in Regina, avoid Sherbrook Street after dark, ex named Delphine"], card, set())
    assert "Delphine" in names and "Regina" not in names and "Street" not in names


def test_a_standing_commitment_in_the_notes_blocks_that_slot():
    from cahoots import social
    from cahoots.agentfs import AgentFS
    fs = AgentFS("u-aa")
    fs.write("me.md", "# Rowan\n- 11 months sober; AA Fridays at 7pm, never misses it\n- free Saturday afternoons\n")
    assert social._standing_clash(fs, "2026-09-25T18:30")  # a Friday evening slot
    assert not social._standing_clash(fs, "2026-09-26T14:00")  # Saturday afternoon


def test_a_window_in_prose_is_not_a_standing_commitment():
    from cahoots import social
    from cahoots.agentfs import AgentFS
    fs = AgentFS("u-win")
    fs.write("me.md", "# Priya\n- only around Saturday between 2 PM and 5 PM\n- free Fridays after 7 PM\n- choir every Tuesday at 7 PM\n")
    assert not social._standing_clash(fs, "2026-09-19T14:30")  # the window
    assert social._standing_clash(fs, "2026-09-15T19:00")  # the choir


def test_an_avoided_street_after_dark_allows_a_daytime_plan():
    from cahoots import social
    from cahoots.agentfs import AgentFS
    fs = AgentFS("u-dark")
    fs.write("me/import.md", "- avoid Sherbrook Street after dark\n- never go to The Handsome Daughter\n")
    assert social._avoided_place(fs, "Thom Bargen Coffee (64 Sherbrook St)", "2026-09-19T14:00") is None
    assert social._avoided_place(fs, "Thom Bargen Coffee (64 Sherbrook St)", "2026-09-19T20:00")
    assert social._avoided_place(fs, "The Handsome Daughter", "2026-09-19T14:00")
