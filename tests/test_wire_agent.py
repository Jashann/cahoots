"""What still holds from the first build's wire layer: the reply parser and the tool-result-aware
message reader that the v2 relay reuses."""
from cahoots.network import _last_user_text, parse_reply


def test_decision_tool_still_finds_the_proposal_after_a_tool_result():
    class A:
        messages = [{"role": "user", "content": [{"text": "Cahoots DM\n{\"thread\": \"t1\", \"text\": \"hi\"}"}]},
                    {"role": "assistant", "content": [{"toolUse": {"name": "my_card", "toolUseId": "t1", "input": {}}}]},
                    {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "{card}"}], "status": "success"}}]}]
    assert _last_user_text(A()).endswith('"text": "hi"}')


def test_parse_reply_never_guesses_an_accept():
    assert parse_reply("I think this is fine, accept")["decision"] == "decline"
    d = parse_reply('Sure. {"decision": "counter", "reason": "later", "fit": 7, "counter": {"start": "20:00"}} done')
    assert d["decision"] == "counter" and d["counter"] == {"start": "20:00"}
