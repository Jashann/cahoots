"""If Bedrock refuses the account and an Anthropic key is configured, the same model runs from Anthropic directly; without a key nothing changes."""


def test_denied_detection():
    from cahoots import person_agent as pa
    assert pa.is_denied(Exception("An error occurred (AccessDeniedException) when calling the Converse operation: account suspended"))
    assert not pa.is_denied(Exception("ThrottlingException: Too many tokens per day"))


def test_fallback_only_with_a_key(monkeypatch):
    from cahoots import person_agent as pa
    monkeypatch.setattr(pa, "ANTHROPIC_API_KEY", "")
    pa.mark_denied()
    assert not pa.bedrock_denied()
    assert type(pa.bedrock(pa.MODEL_ID, 0.4)).__name__ == "BedrockModel"
    monkeypatch.setattr(pa, "ANTHROPIC_API_KEY", "sk-test")
    assert pa.bedrock_denied()
    assert type(pa.bedrock(pa.MODEL_ID, 0.4)).__name__ == "AnthropicModel"
    assert type(pa.bedrock("anthropic", 0.4)).__name__ == "AnthropicModel"
    monkeypatch.setattr(pa, "_DENIED_AT", 0.0)
    pa._denied_file().unlink(missing_ok=True)  # the refusal is remembered on disk too, so a deploy cannot forget it
    assert type(pa.bedrock(pa.MODEL_ID, 0.4)).__name__ == "BedrockModel"


def test_the_suspension_wording_is_a_denial():
    from cahoots import person_agent as pa
    e = Exception("An error occurred (ValidationException) when calling the ConverseStream operation: Error 002: Access to Bedrock models is not allowed for this account")
    assert pa.is_denied(e) and not pa.is_throttled(e)
