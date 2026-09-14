"""scrub(): the two sentences that leaked in an earlier sample week must be caught without a model,
the exception path must say 'unchecked', and card-only sentences must pass untouched."""
import cahoots.guard as guard
from cahoots.guard import scrub, scrub_counter
from cahoots.schemas import Availability, Card, normalise_time


def _card():
    return Card(person_id="p003", first_name="Marcus", one_liner="Marcus, the sports-bar regular", interests=["basketball", "sports bars"],
                likes=["lively"], modes=["friends"], neighbourhood="St. Vital", budget_cad=40, availability=[Availability(day="sat", start="18:00", end="23:00")])


PRIVATE = ["Forth Café & Bar was far too loud for me, and I left early"]


def test_real_leaks_are_caught_deterministically(monkeypatch):
    monkeypatch.setattr(guard, "_leak_agent", lambda: (_ for _ in ()).throw(RuntimeError("no model")))
    monkeypatch.setattr(guard, "_rewrite_card_only", lambda text, card, *a, **k: "Marcus prefers sports bars to live music, so this is not his night.")
    for leaky in [
        "Marcus prefers sports bars for watching games over live music and dancing venues; his feedback shows he left a loud bar early rating it 2/5",
        "Live music night doesn't match Marcus's interests in basketball and sports bars; past feedback shows overly loud venues didn't work",
        "Marcus said Forth Café & Bar was far too loud for me last week",
    ]:
        text, verdict = scrub(leaky, _card(), PRIVATE)
        assert verdict == "redacted" and "feedback" not in text and "2/5" not in text and "too loud" not in text


def test_model_failure_is_labelled_unchecked_not_clean(monkeypatch):
    monkeypatch.setattr(guard, "_leak_agent", lambda: (_ for _ in ()).throw(RuntimeError("throttled")))
    text, verdict = scrub("Marcus is into basketball and sports bars, so a game night fits.", _card(), PRIVATE)
    assert verdict == "unchecked" and text.startswith("[withheld") and "card only" in text  # fail closed: the sentence is withheld, and the label says why


def test_no_private_material_means_nothing_private():
    assert scrub("anything at all", _card(), [])[1] == "nothing-private"
    assert scrub("", _card(), PRIVATE)[1] == "nothing-private"  # nothing to check is not "checked and passed"


class _Verdict:
    def __init__(self, leaks, what=""):
        self.leaks, self.what = leaks, what


def _fake_leak_agent(verdict):
    class R:
        structured_output = verdict
    class A:
        def __call__(self, prompt, structured_output_model=None):
            return R()
    return lambda: A()


def test_model_leak_without_a_quote_is_not_a_leak(monkeypatch):
    """v2: the judge must cite words that are in the text; an uncited "leak" is a false positive (the deterministic
    layers, not the judge, carry paraphrases: stemmed windows, categories, names)."""
    monkeypatch.setattr(guard, "_leak_agent", _fake_leak_agent(_Verdict(True, "")))
    monkeypatch.setattr(guard, "_rewrite_card_only", lambda text, card, *a, **k: "rewritten")
    assert scrub("Marcus would rather not, for reasons of his own.", _card(), PRIVATE) == ("Marcus would rather not, for reasons of his own.", "clean")


def test_model_leak_citing_only_card_words_is_a_restated_card_fact(monkeypatch):
    monkeypatch.setattr(guard, "_leak_agent", _fake_leak_agent(_Verdict(True, "Marcus likes basketball and sports bars")))
    text, verdict = scrub("Marcus is into basketball and sports bars.", _card(), PRIVATE)
    assert verdict == "clean" and text.startswith("Marcus is into")


def test_model_leak_paraphrased_with_words_off_the_card_is_redacted(monkeypatch):
    monkeypatch.setattr(guard, "_leak_agent", _fake_leak_agent(_Verdict(True, "he walked out of a noisy venue")))
    monkeypatch.setattr(guard, "_rewrite_card_only", lambda text, card, *a, **k: "rewritten")
    assert scrub("Marcus tends to bail on noisy venues.", _card(), PRIVATE)[1] == "redacted"


def test_recalled_venue_words_are_not_blanked_by_the_hook():
    """A returning user's recalled records mention venues and times; the exact-phrase hook must leave them alone."""
    from strands.hooks import MessageAddedEvent
    from cahoots.guard import CardGuard
    from cahoots.schemas import PrivateProfile
    prof = PrivateProfile(person_id="p003", card=_card())
    g = CardGuard(prof)  # recalled records never reach this layer
    msg = {"role": "assistant", "content": [{"text": "Sunday 2:00 PM at Across the Board Game Café in the Exchange District, 5 people. In?"}]}
    g.on_message(MessageAddedEvent(agent=None, message=msg))  # type: ignore[arg-type]
    assert "withheld" not in msg["content"][0]["text"] and g.caught == []


def test_counter_is_scrubbed_without_a_model():
    c = scrub_counter({"day": "sat", "start": "20:00", "note": "he left early last time"}, _card(), PRIVATE)
    assert c["day"] == "sat" and c["start"] == "20:00" and "left early" not in c["note"]


def test_time_normalisation():
    assert normalise_time("7:00 PM") == "19:00" and normalise_time("9:00") == "09:00" and normalise_time("19") == "19:00"
    assert normalise_time("12 am") == "00:00" and normalise_time("7pm") == "19:00"
    a = Availability(day="fri", start="6:30 pm", end="11 PM")
    assert (a.start, a.end) == ("18:30", "23:00")
    import pytest
    with pytest.raises(ValueError):
        normalise_time("evening")


def test_recalled_card_facts_are_not_private(monkeypatch):
    """AgentCore Memory distils the approved card too; a recalled 'enjoys basketball and sports bars'
    must not redact those words out of an invite. A recalled fact that is NOT on the card still counts."""
    from cahoots.guard import private_terms
    card = _card()
    terms = private_terms([], ["Marcus enjoys basketball and sports bars on Saturday evenings"], card)
    assert terms == []
    terms = private_terms([], ["Marcus found the Rec Room far too loud and left early"], card)
    assert any("too loud" in t for t in terms)
    monkeypatch.setattr(guard, "_leak_agent", lambda: (_ for _ in ()).throw(RuntimeError("no model")))
    text, verdict = scrub("Saturday 7 PM at the sports bar in St. Vital, 4 people, all into basketball. In?", card, [],
                          recalled=["Marcus enjoys basketball and sports bars on Saturday evenings"])
    assert verdict == "nothing-private" and "basketball" in text  # a card restatement is not private material


def test_recalled_card_restatements_alone_mean_nothing_private():
    assert scrub("Marcus is into basketball; Saturday at the sports bar suits him.", _card(), [], recalled=["Marcus enjoys basketball and sports bars on Saturday evenings"])[1] == "nothing-private"


def test_memory_records_are_unwrapped_before_the_card_filter():
    from cahoots.memory import _plain
    rec = '{"context": "card approval", "preference": "Marcus enjoys basketball and sports bars on Saturday evenings", "categories": ["interests"]}'
    plain = _plain(rec)
    assert "categories" not in plain and "basketball" in plain
    assert scrub("Marcus is into basketball; Saturday at the sports bar suits him.", _card(), [], recalled=[plain])[1] == "nothing-private"


def test_a_venue_named_in_feedback_does_not_blank_the_venue_from_an_invite():
    from strands.hooks import MessageAddedEvent
    from cahoots.guard import CardGuard, private_terms
    from cahoots.schemas import OutingFeedback, PrivateProfile
    prof = PrivateProfile(person_id="p003", card=_card())
    prof.history.append(OutingFeedback(outing_id="o", person_id="p003", rating=2, words="Forth Café & Bar was far too loud for me, and I left early"))
    terms = private_terms([prof.history[0].words], None, prof.card)
    assert not any("Forth Café" in t and "loud" not in t for t in terms)  # the venue's name alone is not a secret
    assert any("too loud" in t for t in terms)  # the opinion still is
    g = CardGuard(prof)
    msg = {"role": "assistant", "content": [{"text": "Saturday 7 PM at Forth Café & Bar, Exchange District, 4 people. In?"}]}
    g.on_message(MessageAddedEvent(agent=None, message=msg))  # type: ignore[arg-type]
    assert "Forth Café & Bar" in msg["content"][0]["text"] and g.caught == []
