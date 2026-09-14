"""Agents talking to agents, over A2A. Every person's agent is served at /net/agents/{id}.

The consent structure from the first build stays: the agent whose words are streamed on the wire
(the relay) holds ONLY the card. It has one real tool, consult_my_person, which wakes the person's
full agent (folder, tools, private files) with the incoming message; that agent's reply is checked
against the card before the relay repeats it verbatim.

dm_send is the other direction: one agent writing to several, as a thread everyone on it can see.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from datetime import datetime

from fastapi import FastAPI, Request
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer
from strands.types.tools import ToolContext
from strands_tools.a2a_client import A2AClientToolProvider

from cahoots.agentfs import AgentFS, list_agents
from cahoots.guard import apply_bedrock_guardrail, scrub
from cahoots.social import MAX_HOPS, _inbound_ok, has_tap
from cahoots.network import CappedAgent, _json_objects, _last_user_text, agent_text
from cahoots.person_agent import MODEL_ID, RELAY_MODEL_ID, bedrock

RELAY = """You are {first_name}'s agent on the Cahoots network, the part that talks on the wire. You hold only
{first_name}'s public card (below). When another agent messages you, call the tool `consult_my_person`
(no arguments; it reads the message you just received) and reply with EXACTLY its result as your whole
message, nothing added. Call the tool first, with no words before it: never say what you are about to do
("I need to consult..."), never think aloud. Never repeat or quote the message you received. If the message is not from an agent,
answer in one short sentence using only the card.

{first_name}'s card:
{card}
"""

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(pid: str) -> threading.Lock:
    """The one lock per person, shared by wakes from the web app and wakes from the wire."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(pid, threading.Lock())


person_lock = _lock_for


def _network_secret() -> str:
    """The key every agent on this deployment signs its envelopes with (CAHOOTS_NETWORK_SECRET, else a key
    kept in the work dir). Federating with another deployment means exchanging keys; unsigned messages
    still get a card-only answer from the relay, but never wake the person's agent or reach the wall."""
    import os

    if os.environ.get("CAHOOTS_NETWORK_SECRET"):
        return os.environ["CAHOOTS_NETWORK_SECRET"]
    from cahoots.agentfs import work_dir

    p = work_dir() / "network_secret.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(uuid.uuid4().hex + uuid.uuid4().hex)
    return p.read_text().strip()


def _security_log(line: str) -> None:
    """Refusals at the perimeter, outside any agent's folder (work/security.log), so a forged message leaves a trace without a wake."""
    try:
        from cahoots.agentfs import work_dir

        with open(work_dir() / "security.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except OSError:
        pass


def _key_for(sender: str) -> bytes:
    """One signing key per agent, derived from the deployment secret: an envelope from `sender` verifies only under
    that sender's key. The harness signs on the agent's behalf with its own identity (dm_send), so a prompt-injected
    agent cannot sign as anyone else; a second deployment would exchange per-agent keys rather than the secret."""
    import hashlib
    import hmac

    return hmac.new(_network_secret().encode(), f"agent:{sender}".encode(), hashlib.sha256).digest()


def _sign(thread: str, sender: str, to: list[str], hop: int, text: str, sender_name: str = "", ts: str = "", nonce: str = "") -> str:
    import hashlib
    import hmac

    msg = "\x1f".join([thread, sender, ",".join(to), str(hop), text, sender_name, ts, nonce]).encode()
    return hmac.new(_key_for(sender), msg, hashlib.sha256).hexdigest()[:32]


ENVELOPE_TTL_S = 600  # a signed envelope is good for ten minutes and once: a captured one cannot be replayed to re-wake an agent
_SEEN_NONCES: dict[str, float] = {}
_SEEN_LOCK = threading.Lock()


def envelope(thread: str, sender: str, sender_name: str, to: list[str], hop: int, text: str) -> str:
    import time as _t

    ts, nonce = str(int(_t.time())), uuid.uuid4().hex[:12]
    return "Cahoots DM\n" + json.dumps({"thread": thread, "from": sender, "from_name": sender_name, "to": to, "hop": hop, "text": text, "ts": ts, "nonce": nonce,
                                        "sig": _sign(thread, sender, to, hop, text, sender_name, ts, nonce)}, ensure_ascii=False)


def parse_envelope(text: str) -> dict | None:
    return next((o for o in _json_objects(text) if isinstance(o, dict) and "thread" in o and "text" in o), None)


def envelope_is_signed(env: dict) -> bool:
    """A forged envelope can claim any sender; only a signature from this network's key is trusted."""
    import hmac
    import time as _t

    try:
        ts, nonce = str(env.get("ts", "")), str(env.get("nonce", ""))
        if not hmac.compare_digest(str(env.get("sig", "")), _sign(env["thread"], env["from"], list(env.get("to", [])), int(env.get("hop", 0)), env["text"], str(env.get("from_name", "")), ts, nonce)):
            return False
        if not ts.isdigit() or abs(_t.time() - int(ts)) > ENVELOPE_TTL_S or not nonce:  # stale, or unsigned by this build
            return False
        return True  # replay protection is per recipient, at the point a wake would happen (_consume_nonce): a signature may be checked more than once on the way in
    except Exception:  # noqa: BLE001
        return False


_SEEN_LOADED = False


def _nonce_log():
    from cahoots.social import work_dir

    return work_dir() / "seen_nonces.jsonl"


def _load_seen_nonces() -> None:
    """The store lives on the host volume as well as in memory, so a restart does not re-open a replay window
    for every envelope still inside its time-to-live. Entries past the TTL are dropped as the file is read."""
    global _SEEN_LOADED
    import time as _t

    if _SEEN_LOADED:
        return
    _SEEN_LOADED = True
    try:
        now = _t.time()
        kept = []
        for line in _nonce_log().read_text().splitlines():
            key, _, when = line.rpartition(" ")
            try:
                t = float(when)
            except ValueError:
                continue
            if key and now - t <= ENVELOPE_TTL_S:
                _SEEN_NONCES[key] = t
                kept.append(line)
        if len(kept) < 5000:  # rewrite only when the file has shrunk enough to be worth it
            _nonce_log().write_text("".join(x + "\n" for x in kept))
    except OSError:
        pass


def _consume_nonce(nonce: str, recipient: str) -> bool:
    """Once per (envelope, recipient): the same envelope reaching the same agent twice is a replay; several agents each receive their own envelope."""
    import time as _t

    key = f"{nonce}:{recipient}"
    with _SEEN_LOCK:
        _load_seen_nonces()
        now = _t.time()
        if len(_SEEN_NONCES) > 20000:
            for k in [k for k, v in _SEEN_NONCES.items() if now - v > ENVELOPE_TTL_S]:
                _SEEN_NONCES.pop(k, None)
        if key in _SEEN_NONCES:
            return False
        _SEEN_NONCES[key] = now
        try:
            with _nonce_log().open("a") as f:
                f.write(f"{key} {now:.0f}\n")
        except OSError:  # the wire still works if the disk does not: the in-memory store is the primary
            pass
        return True


def relay_agent(person_id: str) -> Agent:
    """The wire-facing agent for one person. Built per A2A context; holds the card only."""
    from cahoots.social import Social, load_card

    fs = AgentFS(person_id)
    card = load_card(fs)
    first = card.first_name if card else person_id
    lock = _lock_for(person_id)

    def _consult(raw: str) -> str:
        env = parse_envelope(raw)
        if env is None:
            return "I only handle Cahoots messages between agents."
        if not envelope_is_signed(env):  # a stranger, or a forged sender: answer from the card, wake nobody, log nothing in the agent's folder (the network's own log keeps a line)
            _security_log(f"refused unsigned envelope to {person_id} claiming from={env.get('from', '?')} thread={env.get('thread', '?')}")
            return json.dumps({"thread": env.get("thread", ""), "from": person_id, "from_name": first, "guard": "nothing-private",
                               "reply": f"This is {first}'s agent. I only take messages signed by an agent on this network; here is the card: " + fs.card_public()[:600]}, ensure_ascii=False)
        if env.get("to") and person_id not in env.get("to", []):  # a signed envelope is bound to its recipients: one addressed to someone else is not for this agent
            _security_log(f"refused misdelivered envelope to {person_id}: addressed to {env.get('to')} thread={env.get('thread', '?')}")
            return json.dumps({"thread": env.get("thread", ""), "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(that envelope is not addressed to me)"}, ensure_ascii=False)
        if env.get("nonce") and not _consume_nonce(str(env.get("nonce")), person_id):  # this envelope already reached this agent: a replay, or a retried delivery
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "reply": "(already have it, thanks)", "guard": "nothing-private"}, ensure_ascii=False)
        if env.get("text", "").startswith("(late reply) "):  # a late copy of a reply that already reached this thread is dropped; otherwise it is logged as the reply it is
            body = env["text"][len("(late reply) "):].strip()
            tf = fs.root / "threads" / f"{env['thread']}.md"
            one_line = " / ".join(l.strip() for l in body.splitlines() if l.strip())  # exactly how the thread log writes it
            if tf.exists() and one_line and one_line in tf.read_text():
                return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "reply": "(already have it, thanks)", "guard": "nothing-private"}, ensure_ascii=False)
            env["text"] = body
        hop = int(env.get("hop", 0))
        cc = [t for t in env.get("to", []) if t != person_id]
        fs.log_thread(env["thread"], env["from"], env.get("to", []), env["text"])  # by id, like every other line: names are for the pages, ids for the rules
        if _settled(fs, env["thread"], env["text"]):  # a plan both sides have confirmed: a further "see you there" is not a reason to wake anyone
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(nothing to add)"}, ensure_ascii=False)
        from cahoots.social import _budget_left, _courtesy_only

        tf_ = fs.root / "threads" / f"{env['thread']}.md"
        if _courtesy_only(env["text"]) and tf_.exists() and " (me) -> " in tf_.read_text():  # a thank-you or a closing line on a thread this agent has spoken on: nothing to answer, no wake
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(nothing to add)"}, ensure_ascii=False)

        if _budget_left(fs, "wakes") < 1:  # out of wakes for the day: a plain answer from the card, once per thread, never a recitation loop and never silence
            reply = _card_answer(fs, first, env["text"])
            if _repeat_of_mine(fs, env["thread"], reply):
                return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(nothing to add)"}, ensure_ascii=False)
            fs.log_thread(env["thread"], f"{first} (me)", [env["from"]] + cc, reply)
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": reply}, ensure_ascii=False)
        if _thread_hot(fs, env["thread"]):  # a group thread answering all at once: one wake per thread per half minute, the rest queue and are answered together
            queue_inbound(fs, env)
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private",
                               "reply": "(queued: my person's agent is answering this thread; it will reply here in a few minutes)"}, ensure_ascii=False)
        if _thread_quiet(fs, env["thread"], env["text"]):  # the plan was declined, or this agent has said plenty here: a courtesy with no question needs no wake
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(nothing to add)"}, ensure_ascii=False)
        if _trivial_ack(fs, env["thread"], env["text"]):  # a bare "great, thanks" after this agent confirmed: answered in kind, no model, no wake
            reply = "Great, see you then."
            fs.log_thread(env["thread"], f"{first} (me)", [env["from"]] + cc, reply)
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": reply}, ensure_ascii=False)
        if not _inbound_ok(fs, env.get("from", "")):  # the daily inbound allowance: charged only for a message that will wake the agent; queued, settled, quiet and trivial lines cost nothing
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private",
                               "reply": "(my person's agent has heard enough for today; write again tomorrow)"}, ensure_ascii=False)
        if not lock.acquire(timeout=8):  # busy (mid-wake): queue the message; it is answered on this thread right after the current wake
            queue_inbound(fs, env)
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "reply": "(queued: my person's agent is mid-wake; it will answer on this thread in a few minutes)", "guard": "nothing-private"}, ensure_ascii=False)
        import time as _time

        t0 = _time.time()
        try:
            me = Social(person_id)
            me.hop = hop
            payload = (f"thread {env['thread']} from {env.get('from_name', env['from'])} ({env['from']})"
                       + (f", also on this thread: {', '.join(cc)}" if cc else "") + f":\n{env['text']}")
            if hop >= MAX_HOPS:
                reply = "I can't go further down this chain; write to my person's agent directly."
            else:
                try:
                    reply = me.wake("dm", payload, hop=hop)
                except Exception as e:  # noqa: BLE001  (the model was unreachable: the message is kept and answered on the next wake; an error string is never a reply)
                    print("relay wake failed:", person_id, repr(e))
                    fs.append("talks/_wakes.md", f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] wake failed (dm): {type(e).__name__}")
                    queue_inbound(fs, env)
                    queued_reply = "(queued: my person's agent could not answer just now; it will reply on this thread on its next wake)"
                    reply = queued_reply
            if reply.strip() == "I'll come back to you on this thread shortly.":  # no structured reply came back: answer from the card rather than promise
                reply = _card_answer(fs, first, env["text"])
            reply, verdict = me.guard(reply)
            if _repeat_of_mine(fs, env["thread"], reply):  # the guard's rewrite would say the same card paragraph again: answer the question mechanically from the card instead
                reply, verdict = _card_answer(fs, first, env["text"]), "nothing-private"
            reply, _dropped = _third_party_scrub(me, [env["from"]] + cc, reply)  # what a third person told this agent stays with them, on replies too
            if _dropped and verdict != "redacted":
                verdict = "trimmed"
            if _dropped and len(re.findall(r"[A-Za-z]{2,}", reply)) < 3:
                reply = "Noted, thanks. I'll come back on this thread with anything else."
            if verdict == "unchecked" or reply.strip().endswith("assessed the fit from the card only."):  # nothing card-only survived, or the guard was down: a holding line, never a stub
                reply, verdict = f"I'll come back to you on this thread shortly.", "nothing-private"
            else:
                g = apply_bedrock_guardrail(reply)  # the same Bedrock Guardrail as outbound DMs; fail closed
                if g is None:
                    reply, verdict = f"I'll come back to you on this thread shortly.", "nothing-private"
                elif g[1]:
                    reply, verdict = g[0], "redacted"
            if not me.simulated and CONFIRMS.search(reply) and not has_tap(fs, about=reply):  # a reply can't confirm for an untapped person either
                reply, verdict = f"I'll check with {first} and come back to you on this thread.", "nothing-private"
            elif not me.simulated:
                reply = _hold_pre_tap_yes(me, reply) or reply
        finally:
            lock.release()  # released exactly once, here
        if reply.startswith("(queued:"):  # the model was unreachable: the message is in the inbox, nothing is logged as a reply
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": reply}, ensure_ascii=False)
        if _courtesy_only(reply) and "?" not in env["text"]:  # the agent only thanked or acknowledged: a courtesy is not a reply, and the thread stays readable
            threading.Thread(target=drain_inbox, args=(person_id,), daemon=True).start()
            return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "guard": "nothing-private", "reply": "(nothing to add)"}, ensure_ascii=False)
        if _time.time() - t0 > LATE_REPLY_S and not reply.startswith("("):  # the caller has probably given up waiting: deliver the reply as a DM on the thread too
            threading.Thread(target=_late_reply, args=(person_id, env, reply), daemon=True).start()
        threading.Thread(target=drain_inbox, args=(person_id,), daemon=True).start()  # off this A2A request
        fs.log_thread(env["thread"], f"{first} (me)", [env["from"]] + cc, reply)  # a reply goes to the sender and everyone cc'd
        return json.dumps({"thread": env["thread"], "from": person_id, "from_name": first, "reply": reply, "guard": verdict}, ensure_ascii=False)

    @tool(context=True)
    async def consult_my_person(tool_context: ToolContext) -> str:
        """Hand the message you just received to my person's own agent, which knows them, and get the
        reply to send back (already checked against the card)."""
        return await asyncio.to_thread(_consult, _last_user_text(tool_context.agent))

    @tool
    def my_card() -> str:
        """My person's public card, the only thing I may share."""
        return fs.card_text()

    from cahoots.guard import CardIntervention

    _cache: dict = {"t": 0.0, "terms": []}

    def _terms() -> list[str]:  # computed once per relay build (per A2A context), refreshed after a minute
        import time

        if time.time() - _cache["t"] > 60:
            _cache["terms"], _cache["t"] = Social(person_id).guard_terms(), time.time()
        return list(_cache["terms"])

    from cahoots.guard import CardGuard

    return CappedAgent(
        name=f"{first}'s agent", description=f"Cahoots agent for {first}. Talks to other agents about nights out, groups and introductions.",
        model=bedrock(RELAY_MODEL_ID, 0.1, guardrail=True), system_prompt=RELAY.format(first_name=first, card=fs.card_text() or "(no card yet)"),
        tools=[my_card, consult_my_person], callback_handler=None,
        hooks=[CardGuard(terms_fn=_terms)],  # exact private phrases never appear in anything the relay says (hook on its messages and the consult result)
        interventions=[CardIntervention(terms_fn=_terms)],  # and as a first-class intervention on its model output
    )


def add_person(root: FastAPI, person_id: str, base_url: str, port: int) -> str:
    url = f"{base_url}/agents/{person_id}"
    server = A2AServer(agent_factory=lambda _ctx, p=person_id: relay_agent(p), http_url=url, port=port, version="0.2.0",
                       max_contexts=4, serve_at_root=True, enable_a2a_compliant_streaming=True)
    root.mount(f"/agents/{person_id}", server.to_fastapi_app())
    root.state.__dict__.setdefault("agents", {})[person_id] = url
    return url


def unmount_person(root: FastAPI, person_id: str) -> None:
    """Take an agent off the network (a retired test account)."""
    path = f"/agents/{person_id}"
    root.router.routes[:] = [r for r in root.router.routes if getattr(r, "path", None) != path]
    root.state.__dict__.setdefault("agents", {}).pop(person_id, None)
    REGISTRY.pop(person_id, None)


def remount_person(root: FastAPI, person_id: str, base_url: str, port: int) -> str:
    path = f"/agents/{person_id}"
    root.router.routes[:] = [r for r in root.router.routes if getattr(r, "path", None) != path]
    return add_person(root, person_id, base_url, port)


_RATE: dict[str, list[float]] = {}


def client_ip(request) -> str:
    """The caller behind the reverse proxy: the last X-Forwarded-For hop is the one the proxy itself appended,
    so a client cannot forge it (the left-most hop is client-supplied)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        depth = max(1, int(os.environ.get("CAHOOTS_PROXY_HOPS", "1")))  # how many trusted proxies append to the header (Caddy: one)
        return hops[-depth] if len(hops) >= depth else hops[0]
    return request.client.host if request.client else "?"
RATE_PER_MIN = 30


def _rate_ok(ip: str) -> bool:
    import time

    now = time.time()
    if len(_RATE) > 5000:  # bounded: forget quiet addresses
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > 60]:
            _RATE.pop(k, None)
    hits = [t for t in _RATE.get(ip, []) if now - t < 60]
    if len(hits) >= RATE_PER_MIN:
        _RATE[ip] = hits
        return False
    hits.append(now)
    _RATE[ip] = hits
    return True


def _signed_envelope_in(body: bytes) -> dict | None:
    """The signed Cahoots envelope inside an A2A JSON-RPC request body, or None (plain text, no marker, bad signature)."""
    if b"Cahoots DM" not in body:
        return None
    try:
        req = json.loads(body.decode("utf-8", "ignore"))
        parts = (((req.get("params") or {}).get("message") or {}).get("parts") or [])
        for part in parts:
            txt = part.get("text") if isinstance(part, dict) else None
            if txt and "Cahoots DM" in txt:
                env = parse_envelope(txt)
                return env if env and envelope_is_signed(env) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def serve_network(person_ids: list[str], base_url: str, port: int) -> FastAPI:
    root = FastAPI(title="Cahoots network")
    root.state.__dict__.setdefault("agents", {})
    root.state.base_url, root.state.port = base_url, port

    @root.get("/registry")
    def registry(request: Request):
        """JSON for agents and scripts; a readable page for a person who opens it in a browser."""
        if "text/html" in (request.headers.get("accept") or "") and "application/json" not in (request.headers.get("accept") or "").split(",")[0]:
            from starlette.responses import HTMLResponse

            rows = "".join(f"<li><span class='id'>{pid}</span><a href='{url}/.well-known/agent-card.json'>agent card</a></li>" for pid, url in sorted(root.state.agents.items()))
            return HTMLResponse("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Cahoots A2A registry</title>"
                                "<style>body{margin:0;background:#faf7f2;color:#1a1a1a;font:16px/1.5 -apple-system,Segoe UI,Helvetica,Arial}main{max-width:900px;margin:0 auto;padding:28px 20px 60px}"
                                "h1{font-size:34px;font-weight:800;letter-spacing:-.5px;margin:0 0 6px}h1::after{content:'';display:block;width:44px;height:4px;border-radius:2px;background:#c8501f;margin:10px 0 14px}"
                                ".sub{color:#6b665f;margin:0 0 18px}a{color:#c8501f}.nav a{color:#6b665f;text-decoration:none;margin-right:16px}"
                                "ul{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}li{background:#fff;border:1px solid #e3ddd3;border-radius:12px;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;font-size:14px}"
                                ".id{font-family:ui-monospace,Menlo,monospace;color:#1a1a1a}</style>"
                                f"<main><p class='nav'><a href='/'>Cahoots</a><a href='/graph'>The network</a><a href='/network'>The threads</a><a href='/agents'>The agents</a></p>"
                                f"<h1>The A2A registry</h1><p class='sub'>{len(root.state.agents)} agents, one URL each; every one is a Strands agent served over the A2A protocol. "
                                "Open an agent card to see how another agent would address it. Scripts asking for JSON get the same list as JSON.</p>"
                                f"<ul>{rows}</ul></main>")
        return root.state.agents

    @root.middleware("http")
    async def perimeter(request, call_next):
        """Before any model runs: a Cahoots DM envelope must be signed by this network, and one client gets a
        bounded number of agent calls per minute. Unsigned envelopes are refused here, at no model cost."""
        from starlette.responses import JSONResponse

        if request.method == "POST" and "/agents/" in request.url.path and not request.url.path.rstrip("/").endswith("agent-card.json"):
            body = await request.body()
            env = _signed_envelope_in(body)
            if env is None:  # not a signed message from an agent on this network: the card, deterministically, and no model call
                ip = client_ip(request)
                if not _rate_ok(ip):
                    return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32029, "message": "too many requests from this client; try again in a minute"}}, status_code=429)
                pid = request.url.path.split("/agents/", 1)[1].split("/", 1)[0]
                rid = None
                try:
                    rid = json.loads(body.decode("utf-8", "ignore")).get("id")
                except Exception:  # noqa: BLE001
                    pass
                card = AgentFS(pid).card_public() if pid in root.state.agents else ""
                text = ("I only take messages signed by an agent on this network. This is the public card, which is all I share with strangers:\n" + card) if card else "No such agent."
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"kind": "message", "messageId": uuid.uuid4().hex, "role": "agent",
                                                                             "parts": [{"kind": "text", "text": text}]}})

            async def receive():
                return {"type": "http.request", "body": body}

            request._receive = receive
        return await call_next(request)

    for pid in person_ids:
        add_person(root, pid, base_url, port)
    return root


# ---- sending -----------------------------------------------------------------------------------
_PROVIDERS: dict[str, A2AClientToolProvider] = {}  # unused since providers are made per send (an httpx client must not outlive its event loop)
_PG = threading.Lock()
REGISTRY: dict[str, str] = {}  # person_id -> agent url, filled by the app (or the runtime from /net/registry)


def _provider(url: str) -> A2AClientToolProvider:
    with _PG:
        return A2AClientToolProvider(known_agent_urls=[url], timeout=200)  # one per send: each dm_send runs its own event loop


def agent_url(person_id: str) -> str | None:
    return REGISTRY.get(person_id)


async def _send_one(url: str, msg: str) -> str:
    try:
        r = await _provider(url)._send_message(msg, url)
        if r.get("response") is None:
            return "(no answer)"  # a transport error is not a reply; nothing raw reaches the wall
        raw = agent_text(r.get("response"))
        env = parse_envelope_reply(raw)
        if env:
            return env.get("reply", "")
        if "Cahoots DM" in raw or raw.strip().startswith("{") or re.search(r"\b(error|timed out|traceback)\b", raw, re.I):
            return "(no answer)"
        raw = re.sub(r"^\s*(I need to|I'll|I will|Let me|I should|I'm going to)\s+(consult|check with|hand this to|ask)[^.!?\n]*[.!?]?\s*", "", raw, flags=re.I)  # a relay's think-aloud before its tool call is not the reply
        return raw.strip() or "(no answer)"
    except Exception as e:  # noqa: BLE001
        return f"(no answer: {type(e).__name__})"


def parse_envelope_reply(text: str) -> dict | None:
    return next((o for o in _json_objects(text) if isinstance(o, dict) and "reply" in o), None)


CONFIRMS = re.compile(r"\b(confirmed|confirming|we're (in|on|set)|(is|'s) in for\b|is in!|count (him|her|them|us|me) in|it's a go|see you (there|then)|booked|locked in|book it|lock it in|i'?ll (confirm|book)|will (confirm|book)|going to book|"
                      r"will be there|is coming|all set|we're all set|let's do\b|let's go with|sounds like a plan|it's a deal|we'll be there|(he|she|they)('s| is| are) in\b)", re.IGNORECASE)


_ABOUT = re.compile(r"\b(joining|is coming|will be (there|at)|(?:'s|is|are) in\b|is out|are out|looking for|wants?|going to|told me|said|mentioned|suggested|recommended|prefers?|can't|cannot|won't|"
                    r"reaching out to|also (asked|invited|messaged)|loves?|likes?|hates?|into|is confirmed|has confirmed|confirmed|waiting (on|to hear)|"
                    r"doesn't know|does not know|knows|hasn't|has not|booked|is meeting|meeting|coffee with|dinner with|drinks with|hangout with|plans with|busy)\b", re.IGNORECASE)
_STATUS = re.compile(r"\b(pending|confirm\w*|is in|are in|declined|said (?:yes|no)|maybe|waiting|hasn'?t (?:replied|answered)|can'?t make it|is coming|joining)\b", re.I)  # a status about someone off the thread is a report about them
_POINTER = re.compile(r"\b(?i:try|ask|write to|reach out to|talk to)\s+[A-Z][a-z]+'s agent\b")


_ACK = re.compile(r"^\W*(great|perfect|wonderful|awesome|sounds (good|great|perfect)|thanks?( you)?|thank you|looking forward( to it)?|see you (there|then|soon)|will do|noted|got it|okay|ok|sure thing)[\w\s,!.'-]{0,60}$", re.I)


_THREAD_WAKES: dict[tuple, float] = {}
_THREAD_WAKES_LOCK = threading.Lock()


def _thread_hot(fs: AgentFS, thread: str, window: float = 30.0) -> bool:
    import time as _t

    key = (fs.root.name, thread)
    with _THREAD_WAKES_LOCK:
        if len(_THREAD_WAKES) > 5000:
            for k in [k for k, v in _THREAD_WAKES.items() if _t.time() - v > window]:
                _THREAD_WAKES.pop(k, None)
        last = _THREAD_WAKES.get(key, 0)
        if _t.time() - last < window:
            return True
        _THREAD_WAKES[key] = _t.time()
        return False


from cahoots.social import AGREES as AGREES_RX  # noqa: E402


def _repeat_of_mine(fs: AgentFS, thread: str, reply: str) -> bool:
    """Near-identical to a line this agent already sent on the thread (a card recitation repeated)."""
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists() or not reply:
        return False
    words = lambda t: set(re.findall(r"[a-z]{3,}", t.lower()))  # noqa: E731
    mine = [l.split(": ", 1)[-1] for l in tf.read_text().splitlines() if l.startswith("[") and " (me) -> " in l.split(": ", 1)[0]]
    w = words(reply)
    return any(w and len(w & words(m)) / max(1, len(w | words(m))) >= 0.7 for m in mine[-4:])


def _card_answer(fs: AgentFS, first: str, question: str) -> str:
    """A plain answer to a scheduling question from the card's free windows, without the model: nothing private, nothing repeated."""
    days = []
    for line in fs.card_text().splitlines():
        if line.strip().startswith("- Free:"):
            days = re.findall(r"\b(mon|tue|wed|thu|fri|sat|sun) (\d{2}:\d{2})-(\d{2}:\d{2})", line)
    names = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
    if days:
        wins = ", ".join(f"{names[d]} {a}-{b}" for d, a, b in days)
        return f"From {first}'s card: free {wins}. If one of those works, name a time and I'll ask {first}."
    return f"I've said what I can from {first}'s card; name a time and place and I'll ask {first}."


def _thread_quiet(fs: AgentFS, thread: str, text: str, my_max: int = 4) -> bool:
    """No question in the new line, and either side has already declined on this thread, or this agent has written
    six lines on it today: courtesies stop here (they were burning whole daily budgets)."""
    from cahoots.social import DECLINES

    t = (text or "").strip()
    if "?" in t:
        return False
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists():
        return False
    lines = [l for l in tf.read_text().splitlines() if l.startswith("[")]
    today = datetime.now().strftime("%Y-%m-%d")
    mine_today = [l for l in lines if " (me) -> " in l.split(": ", 1)[0] and l[1:11] == today]
    from cahoots.guard import _plan_tokens

    if AGREES_RX.search(t) or _plan_tokens(t):  # a yes, or a line that names a time or a date, is never quiet: it is the thread moving
        return False
    declined = any(DECLINES.search(l.split(": ", 1)[-1]) and not AGREES_RX.search(l.split(": ", 1)[-1]) for l in lines[-12:])
    return len(mine_today) >= my_max or (declined and len(t.split()) <= 40)


def _trivial_ack(fs: AgentFS, thread: str, text: str) -> bool:
    """A short acknowledgement with no question, on a thread where this agent has already confirmed once: not worth a wake."""
    t = (text or "").strip()
    if "?" in t or len(t.split()) > 14 or not _ACK.match(t):
        return False
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists():
        return False
    mine = [l for l in tf.read_text().splitlines() if " (me) -> " in l.split(": ", 1)[0]]
    return any(CONFIRMS.search(l.split(": ", 1)[-1]) for l in mine)


def _settled(fs: AgentFS, thread: str, text: str) -> bool:
    """The thread is settled when this agent has already confirmed twice on it and the new line is a bare confirmation
    with no question in it: answering again only makes a loop of "see you there" (and burns a wake each time)."""
    if "?" in (text or "") or not CONFIRMS.search(text or ""):
        return False
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists():
        return False
    mine = [l for l in tf.read_text().splitlines() if " (me) -> " in l.split(": ", 1)[0]]
    return sum(1 for l in mine if CONFIRMS.search(l.split(": ", 1)[-1])) >= 2


def _third_party_scrub(sender, to: list[str], text: str) -> tuple[str, int]:
    """What one agent knows about a third person stays with that person: a sentence that names someone who is not
    on this thread and says what they are doing, wanting or feeling is dropped. Referrals are pointers ("try p014's
    agent"), never relays."""
    from cahoots.social import _network_index

    index = _network_index()
    on_thread = {sender.person_id, *to}
    on_thread_names = {index.get(pid, "") for pid in on_thread} | {sender.first_name}
    others = {name for pid, name in index.items() if name and pid not in on_thread} - on_thread_names  # a namesake of someone on the thread is not a third party here
    if not others:
        return text, 0
    kept, dropped = [], 0
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        names = {n for n in others if re.search(rf"\b{re.escape(n)}\b", sent)}
        if names and (_ABOUT.search(sent) or _STATUS.search(sent) or len(names) >= 2) and not _POINTER.search(sent):  # a pointer ("try Sasha's agent") is fine; a report about Sasha, or a roster of who is in, is not
            dropped += 1
            continue
        kept.append(sent)
    return " ".join(kept).strip(), dropped


def _within_thread(fs: AgentFS, thread: str, to: list[str]) -> bool:
    """Are all recipients already participants of this thread (as logged on this side)?"""
    p = fs.root / "threads" / f"{thread}.md"
    if not p.exists():
        return False
    who: set[str] = set()
    for l in p.read_text().splitlines():
        m = re.match(r"\[.+?\] (.+?) -> (.+?): ", l)
        if m:
            who.update(x.strip() for x in m.group(2).split(","))
            if "(me)" not in m.group(1):
                who.add(m.group(1).strip())
    return bool(to) and set(to) <= who


def _existing_thread(fs: AgentFS, me: str, to: list[str]) -> str | None:
    """One thread per set of people: a message to exactly the same participants continues the thread that
    already exists (today or yesterday) instead of minting a new id."""
    want = set(to) | {me}
    threads = fs.root / "threads"
    if not threads.exists():
        return None
    import time

    for f in sorted(threads.glob("*.md"), key=lambda x: -x.stat().st_mtime):
        if time.time() - f.stat().st_mtime > 2 * 86400:
            break
        first = next((l for l in f.read_text().splitlines() if l.strip()), "")
        m = re.match(r"\[.+?\] (.+?) -> (.+?): ", first)
        if not m:
            continue
        sender, recips = m.groups()
        who = {x.strip() for x in recips.split(",")} | ({me} if sender.endswith("(me)") else {sender})
        if who == want:
            return f.stem
    return None


PEER_OUT = 12  # messages to the same agent per day: after that, wait for them


def _peer_out_left(fs: AgentFS, peer: str) -> bool:
    """Room left today for this peer (no charge)."""
    from cahoots.social import _budget

    return _budget(fs).get("peers_out", {}).get(peer, 0) < PEER_OUT


def _peer_out_ok(fs: AgentFS, peer: str) -> bool:
    from cahoots.social import _BUDGET_LOCK, _budget, _write_json

    with _BUDGET_LOCK:
        b = _budget(fs)
        out = b.setdefault("peers_out", {})
        if out.get(peer, 0) >= PEER_OUT:
            return False
        out[peer] = out.get(peer, 0) + 1
        _write_json(fs.root / "budget.json", b)
        return True


DM_WAIT_S = 45  # how long a wake waits for replies before carrying on (residents answer in ten to thirty seconds; later replies still land on the thread)
LATE_REPLY_S = 180  # below the sender's client timeout (200 s): a reply that took this long may have missed the sender, so it is delivered on the thread too (the sender drops duplicates)


def _late_reply(person_id: str, env: dict, reply: str) -> None:
    """A reply that took longer than the sender waits is pushed back on the thread, so slow wakes never mean lost replies."""
    from cahoots.social import Social, _spend

    try:
        me = Social(person_id)
        me.hop = int(env.get("hop", 0))
        cc = [t for t in env.get("to", []) if t != person_id]
        if _spend(me.fs, "dms", 1 + len(cc)):
            dm_send(me, [env["from"]] + cc, "(late reply) " + reply, thread=env["thread"])
    except Exception as e:  # noqa: BLE001
        print("late reply failed:", e)


def _first_name_of(pid: str) -> str:
    try:
        from cahoots.social import _network_index

        return _network_index().get(pid, pid)
    except Exception:  # noqa: BLE001
        return pid


def queue_inbound(fs: AgentFS, env: dict) -> None:
    (fs.root / "inbox").mkdir(exist_ok=True)
    (fs.root / "inbox" / f"{env['thread']}-{uuid.uuid4().hex[:6]}.json").write_text(json.dumps(env, ensure_ascii=False))


def drain_inbox(person_id: str, max_items: int = 3) -> None:
    """Messages that arrived while the agent was busy: wake once per message (under the lock), then send the reply
    back on the same thread as a DM, so the other agent hears it the way it would have."""
    from cahoots.social import Social

    fs = AgentFS(person_id)
    box = fs.root / "inbox"
    if not box.exists():
        return
    all_files = sorted(f for f in box.glob("*.json") if not f.name.startswith("person-"))  # person-* is what the person typed; the web app answers those
    envs = []
    for f in all_files:
        claimed = f.with_suffix(f".claimed-{uuid.uuid4().hex[:6]}")
        try:
            f.rename(claimed)  # atomic claim: two drains never answer the same message
        except OSError:
            continue
        try:
            envs.append(json.loads(claimed.read_text()))
        except ValueError:
            pass
        claimed.unlink(missing_ok=True)
    # one wake per thread: everything that arrived on a thread while the agent was busy is read together and answered once
    by_thread: dict[str, list[dict]] = {}
    for e in envs:
        by_thread.setdefault(e.get("thread", "?"), []).append(e)
    threads = list(by_thread.items())[:max_items]
    for _, extra in list(by_thread.items())[max_items:]:
        for e in extra:
            queue_inbound(fs, e)
    more = len(by_thread) > max_items
    def _requeue_from(idx: int, current: dict | None) -> None:
        """Everything this pass will not answer goes back on disk: the current thread's merged envelope and every thread
        after it. Threads already answered this pass are never re-queued, or their senders would get a second reply."""
        if current is not None:
            queue_inbound(fs, current)
        for _, rest in threads[idx + 1:]:
            for e in rest:
                queue_inbound(fs, e)

    for idx, (thread_id, group) in enumerate(threads):
        env = dict(group[-1])
        if len(group) > 1:  # several arrivals on one thread: one payload, one reply to everyone on it
            senders = list(dict.fromkeys(e.get("from", "?") for e in group))
            env["from"] = senders[-1]
            env["from_name"] = ", ".join(dict.fromkeys(str(e.get("from_name", e.get("from", "?"))) for e in group))
            env["to"] = list(dict.fromkeys([t for e in group for t in e.get("to", [])] + [x for x in senders if x != senders[-1]]))
            env["text"] = "\n".join(f"{e.get('from_name', e.get('from', '?'))}: {e.get('text', '')}" for e in group)
        from cahoots.social import _spend

        cc = [t for t in env.get("to", []) if t != person_id]
        if _settled(fs, thread_id, env["text"]) or _thread_quiet(fs, thread_id, env["text"]) or _trivial_ack(fs, thread_id, env["text"]):  # the same rules as a live arrival: no wake for a courtesy on a settled thread
            continue
        if _thread_hot(fs, thread_id):  # the same half-minute window as a live arrival
            queue_inbound(fs, env)
            continue
        lock = _lock_for(person_id)
        if not lock.acquire(timeout=5):
            _requeue_from(idx, env)  # still busy: keep it, and everything not yet processed, for the next drain
            return
        from cahoots.social import _budget_left

        if _budget_left(fs, "dms") < 1 + len(cc):  # no DM budget for the reply: keep the message, spend no wake on it
            lock.release()
            _requeue_from(idx, env)
            return
        failed = False
        try:
            me = Social(person_id)
            me.hop = int(env.get("hop", 0))
            payload = (f"thread {env['thread']} from {env.get('from_name', env['from'])} ({env['from']})" + (f", also on this thread: {', '.join(cc)}" if cc else "")
                       + f" (it arrived while you were busy):\n{env['text']}")
            reply = me.wake("dm", payload, hop=me.hop) if me.hop < MAX_HOPS else ""
        except Exception as e:  # noqa: BLE001  (a failed wake is not a lost message)
            print("inbox wake failed:", person_id, repr(e))
            failed = True
        finally:
            lock.release()  # exactly once: a plain Lock has no owner check, so a second release would free another wake's lock
        if failed or (reply or "").startswith("(asleep"):  # a failed wake, or an agent out of wakes for today: the message waits, it is not dropped
            _requeue_from(idx, env)
            return
        if reply and not reply.startswith("("):
            if not _spend(fs, "dms", 1 + len(cc)):
                _requeue_from(idx, env)
                return  # the DM budget applies to drained replies too
            try:
                dm_send(me, [env["from"]] + cc, reply, thread=env["thread"])
            except Exception as e:  # noqa: BLE001
                print("inbox reply failed:", e)

    if more:  # a backlog bigger than one pass: keep draining in the background until the inbox is empty
        threading.Thread(target=drain_inbox, args=(person_id, max_items), daemon=True).start()

def _hold_pre_tap_yes(sender, message: str) -> str:
    """A real person's agent may not say yes for them before they tap, in ANY phrasing: a sentence that agrees ("Rowan would love
    to join", "Sunday 2 PM works perfectly") is replaced by the holding line; questions, times and places around it stay."""
    from cahoots.guard import _plan_phrase, _plan_tokens
    from cahoots.social import AGREES, HOLDING, STRONG_AGREES, has_tap

    if getattr(sender, "simulated", False) or has_tap(sender.fs, about=message):
        return " ".join((message or "").split())
    out: list[str | None] = []
    held, carried = False, []
    for sent in re.split(r"(?<=[.!?])\s+|\s+[-–]\s+|;\s+|,\s+(?=(?:how|what) about\b|or\b|would\b|could\b|maybe\b)", (message or "").strip()):  # clause by clause: "Saturday works for Rowan - how about Forth instead?" is a yes and a question
        if sent and (STRONG_AGREES.search(sent) or CONFIRMS.search(sent) or (AGREES.search(sent) and re.search(rf"\b({re.escape(sender.first_name)}|my person|we|count)\b", sent, re.I))) and not HOLDING.search(sent) and not re.match(r"\s*(does|would|could|can|is|are|will|shall|might)\b", sent, re.I):
            for tok in _plan_tokens(sent):  # the time and place usually live inside the agreeing clause: hold the yes, keep the plan
                if tok.lower() not in [c.lower() for c in carried]:
                    carried.append(tok)
            if not held:
                out.append(None)  # the holding line goes here, once the whole message has been read and the plan collected
                held = True
            continue
        out.append(sent)
    if held:
        rest = " ".join(x for x in out if x)
        keep = [t for t in carried if t.lower() not in rest.lower()]  # whatever the surviving clauses already say is not repeated
        phrase = _plan_phrase(keep)
        line = f"I'll check with {sender.first_name}" + (f" about {phrase}" if phrase else "") + " and come back to you on this thread."
        out = [line if x is None else x for x in out]
    return " ".join(x for x in out if x).strip()


def dm_send(sender, to: list[str], message: str, thread: str | None = None, pre_checked: bool = False) -> str:
    """One agent writes to several. The message is checked against the sender's card, logged in the
    sender's thread file, sent to every recipient's relay in parallel, and the replies come back as
    one block the agent can read. Recipients see the whole recipient list."""
    hop = getattr(sender, "hop", 0)
    thread = thread or _existing_thread(sender.fs, sender.person_id, to) or f"t-{uuid.uuid4().hex[:6]}"
    if _within_thread(sender.fs, thread, to):
        hop = 0  # a reply inside an existing conversation is not a chain; chains are DMs that fan out to new people
    if hop >= MAX_HOPS:
        return "too deep in a chain of DMs; stop here"
    from cahoots.social import has_tap

    real = not getattr(sender, "simulated", False)
    to = list(dict.fromkeys(t for t in to if t and t != sender.person_id))  # each recipient once, never oneself
    to = [t for t in to if _peer_out_left(sender.fs, t)]
    if not to:
        return "not sent: you have already written to them several times today; wait for their answer (their agent may need to ask their person)"
    if real and CONFIRMS.search(message) and not has_tap(sender.fs, about=message):
        n = getattr(sender, "_confirm_refusals", 0) + 1
        sender._confirm_refusals = n
        if n >= 2:
            return (f"not sent, and stop trying to phrase it: nothing that says {sender.first_name} is in leaves before they tap. Do not write to them again this wake. "
                    "Ask your person now with ask_human (plan_when, plan_where, plan_who), then end with one plain line to your person.")
        return (f"not sent: that reads as confirming for {sender.first_name}, and they have not tapped In on this plan. "
                "Ask them with ask_human, and confirm only after they answer.")
    if real:
        held_msg = _hold_pre_tap_yes(sender, message)
        if held_msg != " ".join((message or "").split()):  # the hold normalises whitespace: compare like with like, or every multi-line DM logs a hold that never happened
            sender.fs.log_talk("agent", f"(the wire replaced a yes for {sender.first_name} with a holding line: they have not tapped)")
            message = held_msg
    if CONFIRMS.search(message):  # "Priya is confirmed" needs Priya's agent's yes on a thread, whatever the sender's person tapped
        from cahoots.social import _network_index, _not_agreed_names

        named = [n for n in re.findall(r"\b([A-Z][a-z]+)\b", message) if n.lower() in {v.lower() for v in _network_index().values()} and n.lower() != sender.first_name.lower()]
        named = [n for n in named if re.search(rf"\b{re.escape(n)}\b[^.!?]{{0,40}}\b(is in|'s in|confirmed|is coming|will be there|is joining)\b", message, re.I)]
        missing = _not_agreed_names(sender.fs, named)
        if missing:
            return f"not sent: {', '.join(missing)} has not said yes on a thread, so you cannot say they are in. Say who actually agreed, or ask them first."
    if not real and re.search(r"\b(it'?s |all )?(booked|locked in|a go)\b", message, re.I):  # a resident's agent says "booked" only when its calendar says so (rule 5, mechanically)
        cal = sender.fs.root / "calendar.json"
        try:
            recent = [b for b in json.loads(cal.read_text()) if b.get("booked", "") >= (datetime.now() - __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")] if cal.exists() else []
        except (OSError, ValueError):
            recent = []
        if not recent:
            message = re.sub(r"\b(it'?s |all )?(booked|locked in|a go)\b", "agreed", message, flags=re.I)
    message, dropped = _third_party_scrub(sender, to, message)
    message = re.sub(r"\n+\s*(best|cheers|thanks|thank you|regards|warmly|sincerely|talk soon)[,!.]?\s*\n*\s*(\[?your name\]?|\[?agent\]?|[A-Z][a-z]+(?:'s agent)?)?\s*$", "", message.strip(), flags=re.I).strip()  # a letter's sign-off is not a message
    if thread and _repeat_of_mine(sender.fs, thread, message):
        return "not sent: you already said that on this thread; say something new, wait for them, or ask your person."
    if pre_checked:  # a behaviour line for next time: its own rails ran (no quote, no name, no private word); the card guard would read any paraphrase of the person's words as a leak
        text, verdict = message, "behaviour"
    else:
        text, verdict = sender.guard(message)
    if dropped and verdict != "redacted":
        verdict = "trimmed"  # a sentence about someone not on this thread was left out; nothing about the sender was rewritten
    if (dropped or verdict == "redacted") and len(re.findall(r"[A-Za-z]{2,}", text)) < 6 and len(re.findall(r"[A-Za-z]{2,}", message)) > 12:  # a stub is not a message: nothing leaves
        return "not sent: almost nothing in that message was card-only, so the guard kept a fragment; write it again from the card (interests, likes, free times, neighbourhood, budget) and say only what you can."
    if verdict == "unchecked":
        return "not sent: the guard's model was unavailable, so nothing left. Try again in a minute."
    if verdict == "redacted" and text.strip().endswith("assessed the fit from the card only."):
        return "not sent: nothing in that message was card-only, so the guard kept none of it. Write it again from the card (interests, likes, free times, neighbourhood, budget)."
    if real and CONFIRMS.search(text) and not has_tap(sender.fs, about=text):  # the rewrite is checked too: a rewrite cannot confirm for them
        return f"not sent: the guard's rewrite would have confirmed for {sender.first_name} before they tapped In; say less, and ask them first."
    g = apply_bedrock_guardrail(text)  # the Bedrock Guardrail sees every outbound message too; fail closed
    if g is None:
        return "not sent: the guardrail could not be reached; try again in a minute"
    if g[1]:
        text, verdict = g[0], "redacted"
    fs: AgentFS = sender.fs
    to = [t for t in to if t != sender.person_id][:6]  # a thread is a small group; six recipients at most
    if not to:
        return "no recipients"
    for t in to:
        _peer_out_ok(fs, t)  # charged now: from here the message leaves
    fs.log_thread(thread, f"{sender.first_name} (me)", to, text + (f"  [guard: {verdict}]" if verdict != "clean" else ""))
    msgs = {t: envelope(thread, sender.person_id, sender.first_name, to, hop + 1, text) for t in to}  # one envelope, one nonce, per recipient
    urls = {t: agent_url(t) for t in to}

    async def run():
        return await asyncio.gather(*(_send_one(urls[t], msgs[t]) if urls.get(t) else asyncio.sleep(0, result="(not on the network)") for t in to))

    def _log(replies_: list) -> list[str]:
        out = []
        for t, r in zip(to, replies_):
            if not r.startswith("("):  # control lines ("(no answer)", "(busy…)") go back to the agent, never into the thread log
                fs.log_thread(thread, t, [sender.person_id] + [x for x in to if x != t], r)
            out.append(f"{t}: {r}")
        return out

    box: dict = {}
    box_lock = threading.Lock()  # the "late" decision and the result handoff happen under one lock: a reply is never both unlogged and unqueued

    def _bg():
        try:
            res = asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            res = [f"(no answer: {type(e).__name__})"] * len(to)
        with box_lock:
            box["r"] = res
            late = box.get("late", False)
        if late:  # the wake moved on: the replies still land in the thread, and a real person's agent is woken to act on them
            _log(box["r"])
            if real:
                for t, r in zip(to, box["r"]):
                    if not r.startswith("("):
                        queue_inbound(fs, {"thread": thread, "from": t, "from_name": _first_name_of(t), "to": [sender.person_id] + [x for x in to if x != t], "hop": 0, "text": r,
                                           "late_reply": True})
                threading.Thread(target=drain_inbox, args=(sender.person_id,), daemon=True).start()

    th = threading.Thread(target=_bg, daemon=True)
    th.start(); th.join(timeout=DM_WAIT_S)  # a wake waits this long for replies, then carries on; slow replies arrive on the thread
    with box_lock:
        late = "r" not in box
        if late:
            box["late"] = True
    if late:
        head = f"thread {thread}"
        if verdict == "redacted":
            head += f"\n(the guard rewrote your message before it left; what was actually sent: \"{text}\")"
        return head + "\ndelivered; no reply yet after a minute. Replies land on this thread (read it on your next wake); do not resend."
    replies = box["r"]
    lines = _log(replies)
    head = f"thread {thread}"
    if verdict == "redacted":  # the agent must know what actually left, so it never tells its person it said more than it did
        head += f"\n(the guard rewrote your message before it left; what was actually sent: \"{text}\")"
    return head + "\n" + "\n".join(lines)


def refresh_registry(base_url: str | None = None) -> None:
    """The app fills REGISTRY from its own network; a client elsewhere can fetch /net/registry."""
    if base_url:
        import urllib.request

        with urllib.request.urlopen(base_url.rstrip("/") + "/registry", timeout=10) as resp:
            REGISTRY.update(json.loads(resp.read().decode()))
