"""Bedrock model construction shared by every agent (one boto3 session for the whole network; the
optional Bedrock Guardrail for agents that face the wire), and loading the simulated residents.
"""


from __future__ import annotations


import json


import os
import time
import re


from strands.models.bedrock import BedrockModel


from cahoots.schemas import Card, OutingFeedback, PrivateProfile


import boto3


MODEL_ID = os.environ.get("CAHOOTS_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
FALLBACK_MODEL_ID = os.environ.get("CAHOOTS_FALLBACK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")  # when the main model is throttled (daily token quota)
RELAY_MODEL_ID = os.environ.get("CAHOOTS_RELAY_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")  # the relay holds a card and one tool: a small model is enough


ANTHROPIC_API_KEY = os.environ.get("CAHOOTS_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL_ID = os.environ.get("CAHOOTS_ANTHROPIC_MODEL_ID", "claude-sonnet-4-5-20250929")  # the same model, from Anthropic directly, if Bedrock refuses the account
ANTHROPIC_CHEAP_MODEL_ID = os.environ.get("CAHOOTS_ANTHROPIC_CHEAP_MODEL_ID", "claude-haiku-4-5-20251001")
_DENIED_AT = 0.0
DENIED_TTL_S = 1800  # after Bedrock refuses the account, every agent uses the fallback for half an hour before probing Bedrock again


def is_denied(e: BaseException) -> bool:
    """Bedrock refusing the account (suspended, not enabled, access denied), as opposed to a throttle; the cause chain is read too."""
    parts, seen, cur = [], set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur)); parts.append(f"{type(cur).__name__} {cur}")
        cur = getattr(cur, "original_exception", None) or cur.__cause__ or cur.__context__
    text = " ".join(parts)
    return bool(re.search(r"AccessDenied|not authorized|suspended|is not enabled|UnrecognizedClient|InvalidSignature|account.{0,40}(verif|suspend)|not allowed for this account|Access to Bedrock models|Error 002", text, re.I))


def _denied_file():
    from cahoots.agentfs import work_dir

    return work_dir() / "bedrock_denied.json"


def mark_denied() -> None:
    global _DENIED_AT
    _DENIED_AT = time.time()
    try:  # remembered on disk too: a fresh process after a deploy must not claim Bedrock until a wake has proved it
        _denied_file().write_text(json.dumps({"at": _DENIED_AT}))
    except OSError:
        pass


def _denied_at() -> float:
    global _DENIED_AT
    if _DENIED_AT <= 0:
        try:
            _DENIED_AT = float(json.loads(_denied_file().read_text()).get("at", 0) or 0)
        except (OSError, ValueError, AttributeError):
            pass
    return _DENIED_AT


def bedrock_denied() -> bool:
    return bool(ANTHROPIC_API_KEY) and time.time() - _denied_at() < DENIED_TTL_S


def clear_denied() -> None:
    """A Bedrock call succeeded: the account is served again, so the refusal marker goes."""
    global _DENIED_AT
    _DENIED_AT = 0.0
    try:
        _denied_file().unlink(missing_ok=True)
    except OSError:
        pass


def recently_denied(ttl_s: float = 86400) -> bool:
    """For the chrome: Bedrock refused the account and has not served it since. An account suspension lasts days, not
    minutes, so the window is long; a successful Bedrock call clears the marker outright (clear_denied)."""
    return _denied_at() > 0 and time.time() - _denied_at() < ttl_s


def anthropic_model(model_id: str, temperature: float):
    """The same Claude model from Anthropic directly: used only when Bedrock refuses the account and a key is configured."""
    from strands.models.anthropic import AnthropicModel

    wants_big = "sonnet" in model_id or model_id == "anthropic"
    return AnthropicModel(client_args={"api_key": ANTHROPIC_API_KEY}, model_id=ANTHROPIC_MODEL_ID if wants_big else ANTHROPIC_CHEAP_MODEL_ID,
                          max_tokens=4096)  # no sampling params: verified 2026-09-14 on the deployed image, the installed Anthropic SDK's messages.stream() rejects `temperature` even via `params`; docs/LIMITS.md says so


def is_throttled(e: BaseException) -> bool:
    """A throttle, wherever the SDK wrapped it: Strands raises EventLoopException around the model error, with the cause attached."""
    seen, cur = set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if "Throttl" in name or "Throttl" in str(cur) or "Too many tokens" in str(cur) or "ServiceUnavailable" in name:
            return True
        cur = getattr(cur, "original_exception", None) or cur.__cause__ or cur.__context__
    return False


REGION = os.environ.get("AWS_REGION", "us-east-1")


_SESSION = boto3.Session(region_name=REGION)  # one shared session: 60 agents must not mean 60 HTTP client pools
# Bedrock's daily token quota is per region: when the home region's quota for the main model is used up, the same
# model is tried in the next region on the list before any wake drops to the fallback model.
MODEL_REGIONS = [r.strip() for r in os.environ.get("CAHOOTS_MODEL_REGIONS", REGION).split(",") if r.strip()] or [REGION]
_EXHAUSTED: dict[str, float] = {}  # region -> until (epoch): a region whose daily quota ran out is skipped for an hour instead of retried every wake


def mark_exhausted(region: str, hours: float = 1.0) -> None:
    import time as _t

    _EXHAUSTED[region] = _t.time() + hours * 3600


def first_open_region() -> str:
    import time as _t

    for r in MODEL_REGIONS:
        if _EXHAUSTED.get(r, 0) < _t.time():
            return r
    return MODEL_REGIONS[0]
_SESSIONS: dict[str, boto3.Session] = {REGION: _SESSION}


def session_for(region: str | None) -> boto3.Session:
    region = region or REGION
    if region not in _SESSIONS:
        _SESSIONS[region] = boto3.Session(region_name=region)
    return _SESSIONS[region]


def guardrail_config() -> dict:
    """The Bedrock Guardrail for agents whose words go on the wire (data/guardrail.json or env)."""
    gid = os.environ.get("CAHOOTS_GUARDRAIL_ID")
    ver = os.environ.get("CAHOOTS_GUARDRAIL_VERSION", "1")
    if not gid:
        import pathlib

        p = pathlib.Path(__file__).resolve().parents[1] / "data" / "guardrail.json"
        if p.exists():
            d = json.loads(p.read_text())
            gid, ver = d.get("guardrail_id"), str(d.get("guardrail_version", "1"))
    return ({"guardrail_id": gid, "guardrail_version": ver, "guardrail_trace": "enabled", "guardrail_stream_processing_mode": "sync",
             "guardrail_redact_output": True, "guardrail_redact_output_message": "[withheld by guardrail]"} if gid else {})


def bedrock(model_id: str, temperature: float, guardrail: bool = False, region: str | None = None):
    """One shared boto session for every agent. Claude models get prompt caching on the system prompt:
    a person's card and rules are re-sent identically on every proposal in a run. `guardrail=True` adds
    the Bedrock Guardrail (health and finance topics denied, contact details anonymised) for the agents
    that talk to other agents."""
    if ANTHROPIC_API_KEY and (model_id == "anthropic" or bedrock_denied()):
        return anthropic_model(model_id, temperature)  # Bedrock refused the account: the same model from Anthropic directly, no guardrail config to pass
    extra = guardrail_config() if guardrail else {}
    sess = _SESSION if guardrail else session_for(region)  # the Guardrail lives in the home region; a guardrail-bound model stays there
    from botocore.config import Config as _Cfg

    cfg = _Cfg(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=180)  # a quota that is gone for the day is not worth minutes of backoff: fail over instead
    if "anthropic" in model_id:
        from strands.models.model import CacheConfig

        return BedrockModel(model_id=model_id, temperature=temperature, boto_session=sess, boto_client_config=cfg, cache_config=CacheConfig(strategy="auto"), **extra)
    return BedrockModel(model_id=model_id, temperature=temperature, boto_session=sess, boto_client_config=cfg, **extra)


def load_profiles(personas_path: str) -> list[PrivateProfile]:
    """Cards plus each resident's PRIVATE feedback history (data/persona_feedback.json), which the
    guard protects and the inner agent uses. The history never appears in the card."""
    import pathlib

    with open(personas_path) as f:
        cards = [Card(**c) for c in json.load(f)]
    fb_path = pathlib.Path(personas_path).with_name("persona_feedback.json")
    fb = json.loads(fb_path.read_text()) if fb_path.exists() else {}
    out = []
    for c in cards:
        prof = PrivateProfile(person_id=c.person_id, card=c)
        prof.history = [OutingFeedback(**x) for x in fb.get(c.person_id, [])]
        out.append(prof)
    return out
