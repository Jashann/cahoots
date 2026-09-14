"""The A2A reply parser must survive nested counters and prose; card cleaning must strip forbidden categories."""
from cahoots.cards import clean_card
from cahoots.network import parse_reply
from cahoots.schemas import Availability, Card


def test_parse_nested_counter():
    d = parse_reply('Sure. {"decision": "counter", "reason": "Friday no, Saturday yes", "fit": 7, "counter": {"day": "sat", "start": "19:00"}} done')
    assert d["decision"] == "counter" and d["counter"] == {"day": "sat", "start": "19:00"} and d["fit"] == 7


def test_parse_fenced_and_prose():
    assert parse_reply("```json\n{\"decision\":\"decline\",\"reason\":\"no\",\"fit\":2,\"counter\":null}\n```")["decision"] == "decline"
    d = parse_reply("I think this is fine, accept")
    assert d["decision"] == "decline"  # never guess an accept from prose


def test_clean_card_strips_health():
    c = Card(person_id="x", first_name="Ada", one_liner="Ada, the novelist", interests=["novels", "severe shellfish allergy", "hiking"],
             likes=["quiet"], modes=["friends"], neighbourhood="Wolseley", budget_cad=30,
             availability=[Availability(day="sat", start="18:00", end="23:00")],
             notes_for_agents="Loves used bookstores. Has a shellfish allergy so avoids seafood places. New in town.")
    c = clean_card(c)
    assert "allergy" not in " ".join(c.interests).lower() and "allergy" not in c.notes_for_agents.lower()
    assert "used bookstores" in c.notes_for_agents and "new in town" not in c.notes_for_agents.lower()  # being new in town is for the agent, never for a stranger's eyes


def test_clean_card_never_reinserts_a_forbidden_interest():
    c = Card(person_id="x", first_name="Ada", one_liner="Ada, managing a shellfish allergy", interests=["managing a shellfish allergy"],
             likes=["quiet"], modes=["friends"], neighbourhood="Wolseley", budget_cad=30,
             availability=[Availability(day="sat", start="18:00", end="23:00")],
             notes_for_agents="Takes lithium for bipolar II and sees a psychiatrist monthly. Loves used bookstores.")
    c = clean_card(c)
    assert c.interests == [] and "allergy" not in c.one_liner
    assert "lithium" not in c.notes_for_agents and "psychiatrist" not in c.notes_for_agents and "used bookstores" in c.notes_for_agents


def test_decision_tool_reads_the_proposal_from_the_conversation():
    """The wire-facing agent must not echo the proposal as a tool argument (it can be kilobytes of cards)."""
    from cahoots.network import _last_user_text
    class A:  # a stand-in with a Strands-shaped message list
        messages = [{"role": "user", "content": [{"text": "hello"}]},
                    {"role": "assistant", "content": [{"text": "hi"}]},
                    {"role": "user", "content": [{"text": "Outing proposal for your human.\n"}, {"text": '{"outing_id": "o1"}'}]}]
    assert _last_user_text(A()).endswith('{"outing_id": "o1"}')
