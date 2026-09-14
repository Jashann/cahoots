"""Cahoots: one screen. Paste your AI's memory once, approve a card, and from then on talk to your
agent whenever you like; it talks to you when there's a real decision. Nothing is a chat log: the
screen forgets, the agent doesn't.

Judges' windows: /network (the agents' real threads), /agents (the cards on the network).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sys
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from cahoots import wire
from cahoots.agentfs import NEED, PLAN_TALK, ID_RE, ID_STRIP, ROOT, AgentFS, agents_dir, list_agents, work_dir
from cahoots.cards import clean_card, extract_card
from cahoots.memory import shared as _shared_memory
from cahoots.notify import calendar_feed, email_enabled, send_invite_email
from cahoots.person_agent import load_profiles
from cahoots.schemas import Card, Venue
from cahoots.social import _same_question, record_ask, Social, answer_prompt, book_from_question, bootstrap_persona, clear_notes, due_outings, load_card, load_prompts, onboard, push_prompt, save_card

WORK = work_dir()
USERS = WORK / "users"
ROOT_DIR = Path(__file__).resolve().parents[1]
USERS.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE = os.environ.get("CAHOOTS_PUBLIC_BASE", "http://127.0.0.1:8080")
RUNTIME_ARN = os.environ.get("CAHOOTS_RUNTIME_ARN")
ADMIN_TOKEN = os.environ.get("CAHOOTS_ADMIN_TOKEN") or secrets.token_urlsafe(18)
if not os.environ.get("CAHOOTS_ADMIN_TOKEN"):
    print(f"CAHOOTS_ADMIN_TOKEN not set; this process uses {ADMIN_TOKEN}")
_SERVED = int(os.environ.get("CAHOOTS_SERVED_AGENTS", "60"))
TOWN_WAKES_PER_HOUR = int(os.environ.get("CAHOOTS_TOWN_WAKES_PER_HOUR", "2"))  # simulated residents that socialise on their own each hour
_PERSONAS = {c["person_id"]: c for c in json.loads((ROOT / "data" / "personas.json").read_text())}
_FEEDBACK = json.loads((ROOT / "data" / "persona_feedback.json").read_text()) if (ROOT / "data" / "persona_feedback.json").exists() else {}
MEM = _shared_memory()
_WAKING: dict[str, float] = {}
_WAKING_LOCK = threading.Lock()

if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
    except Exception as _e:  # noqa: BLE001
        print("telemetry not set up:", _e)

app = FastAPI(title="Cahoots")


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:  # noqa: BLE001
        return default


# ---- the network: every resident and every real person is served over A2A -----------------------
def _bootstrap() -> list[str]:
    ids = []
    for pid, c in list(_PERSONAS.items())[:_SERVED]:
        bootstrap_persona(Card(**c), _FEEDBACK.get(pid, []))
        ids.append(pid)
    for pid in list_agents():
        if pid.startswith("u-") and (agents_dir() / pid / "card.json").exists():
            ids.append(pid)
    return ids




def _first_name(pid: str) -> str:
    c = load_card(AgentFS(pid)) if (agents_dir() / pid / "card.json").exists() else None
    return c.first_name if c else _PERSONAS.get(pid, {}).get("first_name", pid)


def _user(pid: str) -> dict | None:
    p = USERS / f"{pid}.json"
    u = _load_json(p, None) if p.exists() else None
    if u and "person_id" not in u and u.get("card"):  # a record from the first build
        u["person_id"], u["first_name"] = u["card"]["person_id"], u["card"]["first_name"]
    return u


def _migrate_old_users() -> None:
    """People who approved a card on the first build get an agent folder: card, their feedback as private notes."""
    for p in USERS.glob("*.json"):
        if p.name.endswith(".pending.json"):
            continue
        u = _load_json(p, {})
        if u.get("person_id") and not u.get("edit_key"):  # a first-build record: mint the key its owner needs (read it from the box: work/users/<id>.json)
            u["edit_key"] = secrets.token_urlsafe(12)
            p.write_text(json.dumps(u, indent=2))
        if u.get("card") and u.get("approved") and not (agents_dir() / u["card"]["person_id"] / "card.json").exists():
            fs = AgentFS(u["card"]["person_id"])
            save_card(fs, Card(**u["card"]))
            notes = "\n".join(f"- {f['words']} (rated {f['rating']}/5)" for f in u.get("history", [])) or "- (nothing yet)"
            fs.write("me.md", f"# {u['card']['first_name']}, privately\n\nWhat they said after past outings:\n{notes}\n")
            fs.remember(f"{u['card']['first_name']}: card approved on the first build; past feedback in me.md")
            u.update({"person_id": u["card"]["person_id"], "first_name": u["card"]["first_name"]})
            p.write_text(json.dumps(u, indent=2))


_migrate_old_users()
_IDS = _bootstrap()
NET = wire.serve_network(_IDS, PUBLIC_BASE + "/net", 8080)
app.mount("/net", NET)
wire.REGISTRY.update(NET.state.agents)


def _save_user(u: dict) -> None:
    (USERS / f"{u['person_id']}.json").write_text(json.dumps(u, indent=2))


def _may_act(u: dict | None, request: Request, k: str) -> bool:
    if u is None:
        return False
    if not u.get("edit_key"):
        return False  # a record without a key is nobody's to act on
    return secrets.compare_digest(str(k or request.cookies.get(f"ck_{u['person_id']}", "")), u["edit_key"])


def _wake(pid: str, reason: str, payload: str = "") -> str:
    """One wake at a time per person, on the same lock the wire uses (cahoots.wire.person_lock); a second
    caller waits (up to three minutes) rather than being refused."""
    lock = wire.person_lock(pid)
    if not lock.acquire(timeout=20):
        return "(busy)"  # the caller queues the message; nothing the person says is ever dropped
    with _WAKING_LOCK:
        _WAKING.setdefault(pid, time.time())  # the earliest start wins: the screen's "min so far" never resets mid-wake
    t_start = time.time()
    try:
        words = Social(pid).wake(reason, payload)
        if pid.startswith("u-") and reason in ("daily", "free_evening", "person", "dm_followup", "dm"):
            words = _ensure_question(pid, t_start, words, reason, payload)
        return words
    finally:
        with _WAKING_LOCK:
            _WAKING.pop(pid, None)
        lock.release()
        try:
            _queue_owed_replies(pid, t_start)  # a question another agent asked inside this wake's own replies is owed an answer: queued like any arrival
        except Exception as e:  # noqa: BLE001
            print("owed replies:", pid, repr(e))
        if pid.startswith("u-"):
            try:
                _withdraw_declined(AgentFS(pid))  # a guest's agent said no since the question was asked: the question goes, with a note
                _drop_declined_guests(AgentFS(pid))  # and a guest who said no since the plan was booked comes off the plan
                _cancel_denied_holds(AgentFS(pid))  # and a held plan whose only guest says there is no plan is cancelled, with a note
            except Exception as e:  # noqa: BLE001
                print("withdraw declined:", pid, repr(e))
        threading.Thread(target=_drain_person, args=(pid,), daemon=True).start()  # what the person said meanwhile comes first
        threading.Thread(target=_drain_inbox_after_person, args=(pid,), daemon=True).start()  # then other agents' messages that arrived meanwhile


def _queue_owed_replies(pid: str, since: float) -> int:
    """Replies to this agent's own DMs come back inside the tool result, not as arrivals. If one of them asks this agent a question
    and the wake ended without answering it, nothing would ever wake the agent again for it: two agents could wait on each other
    forever. So after a wake, an unanswered question at the end of any thread the wake touched is queued as an inbound message."""
    fs = AgentFS(pid)
    state_f = fs.root / "talks" / "_followups.json"
    state = _load_json(state_f, {})
    n = 0
    for f in (fs.root / "threads").glob("t-*.md") if (fs.root / "threads").exists() else []:
        if f.stat().st_mtime < since - 5:
            continue
        lines = [l for l in f.read_text().splitlines() if l.startswith("[")]
        if not lines:
            continue
        last_mine = max((i for i, l in enumerate(lines) if re.match(r"\[[^\]]+\] .+? \(me\) -> ", l)), default=-1)
        from cahoots.social import MOVE_WORDS

        for line in lines[last_mine + 1:]:  # everything the others said since this agent last spoke: a question or a move request in the middle of a group thread is owed too
            m = re.match(r"\[([^\]]+)\] (.+?) -> ([^:]+): (.*)$", line)
            if not m or m.group(2).endswith("(me)") or _BOILERPLATE.search(m.group(4)) or m.group(4).startswith("("):
                continue
            if "?" not in m.group(4) and not MOVE_WORDS.search(m.group(4)) and not re.search(r"\b(can'?t make|cannot make|need to (move|change|switch)|doesn'?t work|does not work|no longer)\b", m.group(4), re.I):
                continue
            key = "owed:" + hashlib.sha1(line.encode()).hexdigest()[:12]
            if key in state:
                continue
            state[key] = datetime.now().isoformat(timespec="seconds")
            sender = m.group(2).strip()
            to = [x.strip() for x in m.group(3).split(",") if x.strip()]
            wire.queue_inbound(fs, {"thread": f.stem, "from": sender, "from_name": _first_name(sender), "to": to or [pid], "hop": 0, "text": m.group(4), "owed": True})
            n += 1
    if n:
        state_f.parent.mkdir(exist_ok=True)
        state_f.write_text(json.dumps(state))
    return n


def _wake_bg(pid: str, reason: str, payload: str = "") -> None:
    threading.Thread(target=_wake, args=(pid, reason, payload), daemon=True).start()


def _write_ask_marker(fs: AgentFS, q: dict) -> None:
    """Keep the ask text on the consent question so a 'Take it off' tap knows what to remove."""
    from cahoots.social import _PROMPTS_LOCK, _write_json, prompts_path

    with _PROMPTS_LOCK:
        ps = load_prompts(fs)
        for item in ps:
            if item["id"] == q["id"]:
                item["ask"] = q.get("ask", "")
        _write_json(prompts_path(fs), ps)


def _drain_inbox_after_person(pid: str) -> None:
    """Other agents wait their turn: the inbox drains only once nothing the person typed is still queued (up to a minute)."""
    box = AgentFS(pid).root / "inbox"
    for _ in range(30):
        if not (box.exists() and list(box.glob("person-*.json"))):
            break
        time.sleep(2)
    wire.drain_inbox(pid)


def _queue_person(pid: str, reason: str, payload: str, qid: str = "", yes: bool = False) -> None:
    """The person spoke while their agent was mid-wake: keep it, answer it right after the current wake."""
    box = AgentFS(pid).root / "inbox"
    box.mkdir(exist_ok=True)
    (box / f"person-{uuid.uuid4().hex[:6]}.json").write_text(json.dumps({"reason": reason, "payload": payload, "qid": qid, "yes": yes}))


def _settle_notes(fs: AgentFS, prefix: str) -> None:
    """Notes that start with this text are marked seen: a note that promised something now delivered would only sit there."""
    from cahoots.social import _PROMPTS_LOCK, _write_json, prompts_path

    with _PROMPTS_LOCK:
        ps = load_prompts(fs)
        hit = False
        for q in ps:
            if q.get("kind") == "note" and not q.get("answer") and str(q.get("text", "")).startswith(prefix):
                q["answer"], hit = "seen", True
        if hit:
            _write_json(prompts_path(fs), ps)


def _drain_person(pid: str) -> None:
    box = AgentFS(pid).root / "inbox"
    if not box.exists():
        return
    for f in sorted(box.glob("person-*.json"))[:3]:
        claimed = f.with_suffix(f".claimed-{uuid.uuid4().hex[:6]}")
        try:
            f.rename(claimed)
        except OSError:
            continue
        try:
            item = json.loads(claimed.read_text())
        except ValueError:
            claimed.unlink(missing_ok=True)
            continue
        claimed.unlink(missing_ok=True)
        _wake_then_note(pid, item["reason"], item["payload"], item.get("qid", ""), item.get("yes", False))
        _settle_notes(AgentFS(pid), "Got it. I'm mid-conversation")  # the promise to answer is kept: the promise itself leaves the screen


def _answer_worker(pid: str, q: dict, answer: str, yes: bool) -> None:
    """A tap: book mechanically (under the person lock, waiting as long as it takes), then wake the agent to confirm with
    the others. The booking never depends on the model."""
    booked_line = ""
    if yes and q.get("plan"):
        lock = wire.person_lock(pid)
        with _WAKING_LOCK:
            _WAKING.setdefault(pid, time.time())
        got = lock.acquire(timeout=900)  # the tap must land even behind a long wake; a wake that hangs longer than fifteen minutes does not get to block a booking forever
        if not got:
            print("answer worker: lock held for fifteen minutes, booking without it:", pid)
        try:
            booked_line = book_from_question(Social(pid), q)
        finally:
            if got:
                lock.release()
    payload = f"Q: {q['text']}\nA: {answer}" + (f"\n\nThe tap booked it already: {booked_line}. Confirm with the other agents on the thread and close any other open thread; do not book again." if booked_line.startswith("booked") else "")
    if q.get("kind") == "feedback":  # how an outing went: the words are in the folder already; the wake only decides what behaviour to pass on
        try:
            touched = _record_feedback(AgentFS(pid), q, answer)
        except Exception as e:  # noqa: BLE001
            print("record feedback:", pid, repr(e))
            touched = []
        o = q.get("outing") or {}
        payload += (f"\n\nThis is how the outing went ({o.get('where', 'the plan')}, {str(o.get('when', ''))[:10]}, with {re.sub(ID_STRIP, '', str(o.get('who', '')))}). "
                    f"Their words are already written, privately, into {', '.join(touched) or 'the folder'}: do not book anything and do not repeat them anywhere. "
                    "If there is something the other person's agent could act on next time, call pass_behaviour(to=<their id>, behaviour=<one line about next time>) for that agent: "
                    "behaviour, never the quote, never a name. Then reply in one warm line.")
    _wake_then_note(pid, "answer", payload, q["id"], yes and not booked_line.startswith("booked"))


_HARNESS_WORDS = re.compile(r"\b(calendar system|the guard\b|guard (?:kept|blocked|rewrote|stripped|caught)|rewritten by|got rewritten|tool (?:call|error|result|refus\w+)|harness|thread id|"
                            r"t-[0-9a-f]{6}|q-[0-9a-f]{6}|system error|an error (?:on|in) my|error I need|refused by|was refused|my tools\b|function call|"
                            r"free windows|\((?:mon|tue|wed|thu|fri|sat|sun) \d{2}:\d{2}-\d{2}:\d{2}|not (?:asked|sent|booked):|not yours to write|plan_when|plan_who|ask_human|calendar\('|\bqid\b)\b", re.I)
# "work around", "the system" and a bare "rail" are ordinary English: they used to throw away the whole reply, including
# an agent's own refusal to leak. The machinery words that remain drop their own sentence, not the answer around them.


def _off_screen(reply: str) -> tuple[str, str]:
    """The reply with any sentence that talks about the machinery removed, and the removed sentences.
    Losing a sentence is right; losing a person's whole answer because it said "work around" is not."""
    sents = re.split(r"(?<=[.!?])\s+", (reply or "").strip())
    keep = [x for x in sents if x and not _HARNESS_WORDS.search(x)]
    dropped = [x for x in sents if x and _HARNESS_WORDS.search(x)]
    return " ".join(keep).strip(), " ".join(dropped).strip()


def _wake_then_note(pid: str, reason: str, payload: str, qid: str = "", yes: bool = False) -> None:
    """A wake the person triggered from the one screen: the reply is logged and left on the screen as a note
    until they act again (the screen forgets; the talks log keeps it). A yes that produced no booking gets one
    follow-up wake that must book it: the calendar is the product, not the prose."""
    with _WAKING_LOCK:
        mine = pid not in _WAKING  # another wake may already own the "on it" flag; only the owner clears it
        _WAKING.setdefault(pid, time.time())
    fs = AgentFS(pid)
    before = len(_load_json(fs.root / "calendar.json", []))
    t_wake = time.time()
    try:
        raw = _wake(pid, reason, payload)
        if raw == "(busy)":  # mid-wake: keep the message, answer it as soon as the current wake ends
            _queue_person(pid, reason, payload, qid, yes)
            if mine:
                with _WAKING_LOCK:
                    _WAKING.pop(pid, None)
            return
        reply = re.sub(r"\*\*?|__|^#+\s*", "", raw, flags=re.M).strip()  # plain words on the screen, no markdown
        reply = re.sub(r"\(?\bnot (sent|asked|booked|written|appended|cancelled|confirmed)\b[^.\n]*[.\n]?\)?", "", reply).strip()  # a tool's refusal is for the agent, never words on the person's screen
        reply = re.sub(r"\s{2,}", " ", reply)
        reply = re.sub(r"\bpass(?:ed|ing)? (?:that|the|your|this) (?:feedback|comments?|words)(?: along| on)?\b", "passed on what would help next time", reply, flags=re.I)  # behaviour left, never the words
        if _HARNESS_WORDS.search(reply):  # the machinery is for the log, never for the person's screen
            kept, dropped = _off_screen(reply)
            fs.log_talk("agent", f"(kept off the screen: {dropped[:240]})")
            reply = kept or _mechanical_closer(fs, t_wake)  # only when nothing they could read survives
        if yes and len(_load_json(fs.root / "calendar.json", [])) == before:
            follow = _wake(pid, "answer", f"{payload}\n\nYou answered without booking. Nothing is on the calendar for this plan. Call calendar('book', when=<ISO date and time>, "
                                          f"where=<the place>, who=<first names>, note=<one line>, approved_by='{qid}') now, from the question's details, then reply in one short line "
                                          f"saying exactly what is booked (or why it could not be).")
            if follow.strip() and follow != "(busy)":
                reply = re.sub(r"\*\*?|__|^#+\s*", "", follow, flags=re.M).strip()
    except Exception as e:  # noqa: BLE001  (a wake must never fail silently: the screen says so, and the log keeps the reason)
        print("wake failed:", pid, reason, repr(e))
        fs.append("talks/_wakes.md", f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] wake failed ({reason}): {type(e).__name__}")
        if reason in ("person", "answer") and "retry" not in payload[-200:]:  # kept and retried on the loop's next pass, once
            _queue_person(pid, reason, payload + "\n\n(retry: the first attempt could not reach the model)", qid, yes)
        reply = "I couldn't get to that just now. I've kept what you said and I'll pick it up in a few minutes."
    finally:
        if mine:  # another wake's flag is theirs to clear
            with _WAKING_LOCK:
                _WAKING.pop(pid, None)
    if not reply or reply.startswith("(") or reply.startswith("Noted. I've updated my notes"):  # the wake ended on a tool call, a budget line or the model's canned line: a closer built from the folder instead
        reply = _mechanical_closer(fs, t_wake)
    if reason in ("person", "answer"):  # a place their notes avoid, a time outside their windows: the person hears why, in plain words, instead of a plan silently not happening
        try:
            from cahoots.social import _refusals_since

            since_iso = datetime.fromtimestamp(t_wake).isoformat(timespec="seconds")
            said = [r for r in _refusals_since(fs, since_iso) if not any(w in reply.lower() for w in re.findall(r"[a-z]{5,}", r.lower())[:2])]
            if said:
                reply = reply.rstrip() + " " + " ".join(f"I didn't go with that one. {r}." for r in said)
        except Exception:  # noqa: BLE001
            pass
    try:  # nothing on the calendar: the screen may not claim a plan exists
        upcoming = [b for b in _load_json(fs.root / "calendar.json", []) if b.get("when", "") >= datetime.now().strftime("%Y-%m-%dT%H:%M")]
        claim = re.compile(r"\b(booked|you're (all )?set|all set|see you (there|then)|is confirmed|it's confirmed|in about \d+ minutes)\b", re.I)
        if not upcoming and claim.search(reply):  # the sentence that claims a plan goes; the rest stays; the truth is added in one line
            kept = [x for x in re.split(r"(?<=[.!?])\s+", reply) if not claim.search(x)]
            reply = (" ".join(kept).strip() + " " if kept else "") + "Nothing is on your calendar yet; a plan books only when you tap In."
    except Exception:  # noqa: BLE001
        pass
    try:  # a held plan is not "all set": the words on the screen follow the calendar's own status
        if any(b.get("status") == "held" for b in _load_json(fs.root / "calendar.json", [])) and re.search(r"\b(all set|you're set|it's booked|it is booked|confirmed)\b", reply, re.I):
            reply = re.sub(r"\b(all set|you're set|it's booked|it is booked|confirmed)\b", "held until the others confirm", reply, flags=re.I)
    except Exception:  # noqa: BLE001
        pass
    fs.log_talk("agent", reply)
    open_now = [q for q in load_prompts(fs) if q.get("options") and not q.get("answer") and q.get("kind") != "consent"]
    asks_person = re.search(r"\?\s*$", reply.strip()) and re.search(r"\b(does that work|want me to|shall i|should i|would you like|would you rather|or would you|ok with you|are you (in|up for)|sound good|interested)\b", reply, re.I)
    if asks_person and len(open_now) < 3:  # the product's promise: a decision comes with buttons, never as prose ending in "?"
        push_prompt(fs, "question", reply, ["Yes, go ahead", "No, not that"], log=False)
    else:
        if asks_person:  # three decisions already wait: the trailing question is not left dangling as prose
            reply = re.sub(r"[^.!?]*\?\s*$", "", reply).strip() or reply
        push_prompt(fs, "note", reply, log=False)


def _reflect_via_runtime(pid: str) -> bool:
    """The person's own reflection runs on Amazon Bedrock AgentCore Runtime: the folder goes in, the agent
    reads the import and the day's logs, rewrites me.md and MEMORY.md, and the changed files come back.
    Reflection never DMs, so it is safe to run anywhere. Falls back to a local wake if the runtime is off."""
    if not RUNTIME_ARN:
        return False
    try:
        import boto3
        from botocore.config import Config

        fs = AgentFS(pid)
        client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                              config=Config(read_timeout=840, connect_timeout=30, retries={"max_attempts": 0}))  # a reflection can take ten minutes on a busy day
        fs.append("talks/_wakes.md", f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] reflect on agentcore-runtime: started")
        lock = wire.person_lock(pid)
        with lock:  # a consistent snapshot; the Runtime call itself runs without the lock
            before = fs.snapshot()
            payload = json.dumps({"action": "reflect", "person_id": pid, "files": before}).encode()
        resp = client.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex[:8], payload=payload, contentType="application/json", accept="application/json")
        body = resp["response"].read() if hasattr(resp.get("response"), "read") else resp.get("response")
        data = json.loads(body)
        files = {k: v for k, v in data.get("files", {}).items() if k in ("me.md", "MEMORY.md") or k.startswith("people/")}  # reflection rewrites notes, never logs or state
        with lock:
            now_ = fs.snapshot()
            merged = {}
            for k, v in files.items():
                if now_.get(k) == before.get(k):
                    merged[k] = v
                else:  # the agent wrote this file while the reflection ran: its version stays, and the reflection's new lines are added under it (no lost update either way)
                    cur = now_.get(k) or ""
                    have = set(cur.splitlines())
                    extra = [l for l in (v or "").splitlines() if l.strip() and l not in have and not l.startswith("#")]
                    if extra:
                        merged[k] = cur.rstrip("\n") + "\n\n## From today's reflection (AgentCore Runtime)\n" + "\n".join(extra) + "\n"
            files = merged
            fs.restore(files)
            fs.append("talks/_wakes.md", f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] reflect on agentcore-runtime: {len(files)} files updated, {int(data.get('recalled', 0) or 0)} AgentCore Memory records recalled")
        return True
    except Exception as e:  # noqa: BLE001
        print("runtime reflect fell back:", e)
        return False


_DAILY_DONE: dict[str, str] = {}


def _daily(pid: str, payload: str) -> None:
    """Reflect (on the Runtime when configured), then socialise (here, where the network is). The Runtime call
    runs off the person lock: it works on a snapshot, and only the restore of the changed files takes the lock."""
    with _WAKING_LOCK:
        if _WAKING.get(pid):
            return  # already awake: the day is not marked, so the loop tries again on its next pass
        _WAKING[pid] = time.time()
    _DAILY_DONE[pid] = datetime.now().strftime("%Y-%m-%d")  # marked only once the day's wake actually starts
    t_flag = _WAKING[pid]  # the clock the screen shows: kept across the reflect, the wake and the trailing reflect
    fs = AgentFS(pid)
    first = not any((fs.root / "threads").glob("t-*.md")) if (fs.root / "threads").exists() else True  # a first wake (never talked to anyone): meet people first, reflect on it after
    reflected = False
    if not first:
        try:
            reflected = _reflect_via_runtime(pid)
        finally:
            with _WAKING_LOCK:
                _WAKING.pop(pid, None)
    else:
        with _WAKING_LOCK:
            _WAKING.pop(pid, None)
    t_start = time.time()
    with _WAKING_LOCK:
        _WAKING.setdefault(pid, t_flag)
    try:
        words = _wake(pid, "daily", payload + (" (Your notes were just refreshed by your reflection on AgentCore Runtime; read MEMORY.md again.)" if reflected else ""))
    except Exception as e:  # noqa: BLE001  (the model was unreachable: the day is unmarked so the loop tries again, and the screen says so)
        print("daily wake failed:", pid, repr(e))
        fs.append("talks/_wakes.md", f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] wake failed (daily): {type(e).__name__}")
        _DAILY_DONE.pop(pid, None)
        with _WAKING_LOCK:
            _WAKING.pop(pid, None)
        push_prompt(fs, "note", "I couldn't get out to the other agents just now. I'll try again in a few minutes.", log=False)
        return
    ps = load_prompts(fs)
    if not any(q.get("options") and not q.get("answer") and q.get("kind") != "consent" for q in ps) and _fresh_yes(fs, since=t_start):  # an agent said yes and the person has no question: that is the outcome, ask now
        _wake(pid, "follow_up", "One of the agents you wrote to has said yes to a time and place (read the threads changed in the last hour). Turn it into ONE question for your person with ask_human, "
                             "carrying plan_when, plan_where and plan_who (only people whose agent said yes), at a place and time that fit their notes and free windows. Do nothing else.")
        ps = load_prompts(fs)
    just_said = [q for q in ps if q.get("kind") == "note" and q.get("t", "") >= (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")]
    if not any(q.get("options") and not q.get("answer") and q.get("kind") != "consent" for q in ps) and not just_said:  # a wake that asked nothing still leaves the person a line
        n = len(list((fs.root / "threads").glob("*.md")))
        push_prompt(fs, "note", f"I've been out talking to other agents ({n} conversation{'s' if n != 1 else ''} so far). Nothing to decide yet; I'll come back when a plan forms.", log=True)
    if first and RUNTIME_ARN:  # now there is something to reflect on: the import plus the first day's talks
        with _WAKING_LOCK:
            _WAKING.setdefault(pid, t_flag)
        try:
            _reflect_via_runtime(pid)
        finally:
            with _WAKING_LOCK:
                _WAKING.pop(pid, None)


def _ensure_question(pid: str, since: float, words: str, reason: str = "", payload: str = "") -> str:
    """A yes from another agent on a thread touched by this wake, and no question on the person's screen: the wake
    did not finish its job. One follow-up (per thread, ever) that must ask, with the plan checked like any other."""
    fs = AgentFS(pid)
    open_qs = [q for q in load_prompts(fs) if q.get("options") and not q.get("answer") and q.get("kind") != "consent"]
    fresh = [t for t in _fresh_yes_threads(fs, min(since, time.time() - 3600)) if not any(t in str(q.get("thread", "")) or _same_question(q.get("text", ""), _thread_tail(fs, t)) for q in open_qs)]  # a question about THIS plan blocks; an unrelated one does not
    if not fresh and reason == "person" and (NEED.search(payload or "") or PLAN_TALK.search(payload or "")) and re.search(r"\b(want me to|shall i|should i|would you like me to|let me know if|does that work|would you rather|or would you)\b|\?\s*$", words or "", re.I):
        # the ask came back as prose ending in a question to the person: the product promises an introduction and two buttons
        state_f = fs.root / "talks" / "_followups.json"
        state = _load_json(state_f, {})
        key = "ask:" + hashlib.sha1((payload or "").encode()).hexdigest()[:10]
        if key in state:
            return words
        state[key] = datetime.now().isoformat(timespec="seconds")
        state_f.parent.mkdir(exist_ok=True)
        state_f.write_text(json.dumps(state))
        follow = Social(pid).wake("follow_up", f"You answered {_first_name(pid)}'s ask with a question back to them ('{(words or '')[-160:]}'). "
                                  "They should get buttons, not a question: if you found the right person, write to that person's agent now to propose a concrete time and place inside your person's free windows "
                                  "(calendar('free')), and when their agent says yes, ask your person with ask_human carrying plan_when, plan_where and plan_who. If the other agent cannot answer now, "
                                  "ask your person one In? for the time you proposed, saying the other side is still confirming. A note that ends in 'want me to?' is not an outcome.")
        return follow or words
    if not fresh:
        return words
    state_f = fs.root / "talks" / "_followups.json"
    state = _load_json(state_f, {})
    todo = [t for t in fresh if f"ask:{t}" not in state]
    if not todo:
        return words
    for t in todo:
        state[f"ask:{t}"] = datetime.now().isoformat(timespec="seconds")
    state_f.parent.mkdir(exist_ok=True)
    state_f.write_text(json.dumps(state))
    follow = Social(pid).wake("follow_up", "Agents on the threads " + ", ".join(todo) + " have said yes to a time and place, and your person has no question on their screen. "
                              "Turn it into ONE question with ask_human now, carrying plan_when, plan_where and plan_who (only people whose agent said yes), at a place and time that fit your person's notes and free windows "
                              "(calendar('free')); if the agreed time is outside their windows, propose one inside them to the agents first. A note is not an outcome.")
    if not any(q.get("options") and not q.get("answer") and q.get("kind") != "consent" for q in load_prompts(fs)):  # the model still asked nothing: the question is built from the thread itself
        for t in todo:
            if _mechanical_ask(pid, t):
                break
    return follow or words


def _thread_tail(fs: AgentFS, thread: str) -> str:
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists():
        return ""
    lines = [l.split(": ", 1)[-1] for l in tf.read_text().splitlines() if l.startswith("[")]
    return " ".join(lines[-2:])


def _fresh_yes_threads(fs: AgentFS, since: float) -> list[str]:
    from cahoots.social import AGREES, DECLINES

    out = []
    for f in (fs.root / "threads").glob("t-*.md") if (fs.root / "threads").exists() else []:
        if f.stat().st_mtime < since:
            continue
        lines = [l for l in f.read_text().splitlines() if l.startswith("[")]
        if not lines:
            continue
        for line in lines[-4:]:  # a yes from another agent in the last few lines, even if this agent already replied to it
            m = re.match(r"\[[^\]]+\] (.+?) -> [^:]+: (.*)$", line)
            if m and not m.group(1).endswith("(me)") and not _BOILERPLATE.search(m.group(2)) and (
                    (AGREES.search(m.group(2)) and not DECLINES.search(m.group(2))) or _OFFER.search(m.group(2))):  # a yes, or a counter-offer (even inside a polite no)
                out.append(f.stem)
                break
    return out


_BOILERPLATE = re.compile(r"I only take messages signed|here is the card:|\(already have it|\(nothing to add\)|\(queued:|assessed the fit from the card only", re.I)
_OFFER = re.compile(r"\b(instead|how about|what about|could do|would work|works better|another time|other time|different (time|day)|or later|or earlier|any ?time after|free (on |at |next |this |from |after )?(mon|tue|wed|thu|fri|sat|sun)\w*|available (on |at |next |this |from |after )?(mon|tue|wed|thu|fri|sat|sun)\w*|(after|from|before|around|by|at) \d{1,2}(:\d\d)?\s?(am|pm)|(mon|tue|wed|thu|fri|sat|sun)\w* (morning|afternoon|evening)s?|(mon|tue|wed|thu|fri|sat|sun)\w*,? (at )?\d{1,2}(:\d\d)?\s?(am|pm)|\d{1,2}(:\d\d)?\s?(am|pm) (or later|onwards|or after))\b", re.I)  # a counter-offer names a time: it is a decision for the person, even inside a polite no


_CLOSED = re.compile(r"\b(no response needed|all set|no plan to confirm|there'?s been a mix-?up|not a match|isn'?t a match|we'?re on the same page that this isn'?t|didn'?t agree|never agreed|let'?s leave it|leave it there)\b", re.I)
_VENUE_REFUSED = re.compile(r"\b(isn'?t|is not|wouldn'?t be|won'?t be|not) (?:quite |really )?(?:the )?right (?:fit|place|spot|kind)|not (?:their|her|his) (?:thing|scene|style)|rather (?:not|somewhere else)|somewhere (?:else|livelier|quieter|more)\b", re.I)
_NEG = re.compile(r"\b(can'?t|cannot|can not|couldn'?t|won'?t|unable|not|no|isn'?t|aren'?t|doesn'?t|don'?t|out|busy|impossible|never)\b", re.I)


_BOUNDARY = re.compile(r"\b(ends?|ending|finish(?:es|ing)?|until|till|up to|before|after|no later than|earliest|latest|from|starts?|starting)\b[^.\n]{0,14}\d", re.I)
_OUTSIDE = re.compile(r"\b(outside|beyond|past)\b", re.I)


def _offered_clauses(line: str) -> str:
    """The parts of a reply that offer something: clauses without a negation. "Can't do Tuesday 3 PM, but Thursday 6 works" keeps only
    "but Thursday 6 works", so a declined time is never read as the offered one."""
    out = []
    line = re.sub(r"\([^)]*\)", " ", line or "")  # an aside that restates their hours ("(free afternoons, ends at 6 PM)") offers nothing
    for clause in re.split(r"[,;.!?]| but | though | although | however ", line):
        clause = re.sub(r"\b(?:between\s)?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\s?(?:[-–]|to|until|till|and)\s?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\b", " ", clause, flags=re.I)
        if _BOUNDARY.search(clause) or _OUTSIDE.search(clause):
            continue  # "ends at 6 PM" names the edge of a window, not a time on offer; "outside her availability" is a no
        if clause.strip() and not _NEG.search(clause):
            out.append(clause.strip())
    return " ".join(out)


def _mechanical_ask(pid: str, thread: str) -> bool:
    """The model was told to ask and did not: build the question from the thread's own yes line (time, place, who),
    checked like any other plan, so the person gets buttons whatever the model did."""
    from cahoots.guard import _plan_tokens
    from cahoots.social import AGREES, DECLINES, _constraint_clash, _known_venue, _outside_windows, _venue_name, push_prompt

    fs = AgentFS(pid)
    tf = fs.root / "threads" / f"{thread}.md"
    if not tf.exists():
        return False
    lines = [l for l in tf.read_text().splitlines() if l.startswith("[")]
    yes = None
    offered = False
    for line in reversed(lines[-6:]):
        m = re.match(r"\[[^\]]+\] (.+?) -> [^:]+: (.*)$", line)
        if not m or _BOILERPLATE.search(m.group(2)):
            continue
        if m.group(1).endswith("(me)"):
            if DECLINES.search(m.group(2)) or _CLOSED.search(m.group(2)):
                return False  # our own agent has since said not this time: nothing on this thread is a plan to put on the screen
            continue
        if _CLOSED.search(m.group(2)):
            return False  # "no response needed, all set" / "no plan to confirm here": the other side closed the thread
        if AGREES.search(m.group(2)) and not DECLINES.search(m.group(2)):
            yes = m
            break
        if _OFFER.search(m.group(2)):  # "Monday doesn't work; Tuesday 10 to 3?" is a counter-offer: the person decides on the offered time
            if _VENUE_REFUSED.search(m.group(2)):
                return False  # they refused the kind of place this side proposed: the time may be theirs, the place is not, and "as before" would put their no on the screen as a yes
            yes, offered = m, True
            break
    if not yes:
        return False
    who = _first_name(yes.group(1))
    text_all = " ".join(l.split(": ", 1)[-1] for l in lines[-8:])
    yes_text = yes.group(2) if not offered else _offered_clauses(yes.group(2))  # a counter-offer's time comes from the clause that offers, never the one that declines
    if offered and not yes_text.strip():
        return False
    toks = _plan_tokens(text_all)
    venue = next((t for t in toks if _known_venue(t)), None)
    when = None
    for ctx in (yes_text, text_all):  # the line that said yes decides the time when it names one; the whole tail only fills in the day
        for t in _plan_tokens(ctx) or toks:
            try:
                when = _parse_plan_time(t, ctx if re.search(r"\d\s?(am|pm)\b|\d:\d\d", ctx, re.I) else text_all)
                if when:
                    break
            except Exception:  # noqa: BLE001
                continue
        if when:
            break
    card = load_card(fs)
    if when and not _words_name_plan_web(yes_text, when):  # the time must come from the line that said yes or offered; a window's edge in the tail is not their offer
        return False
    if when and _NEG.search(" ".join(c for c in re.split(r"[,;.!?]| but ", yes.group(2)) if re.search(r"\d", c) and _words_name_plan_web(c, when))):  # the clause that names this time must not be a negation
        return False
    if when and not venue:
        from cahoots.social import _standing_clash
        if _standing_clash(fs, when):
            return False
        outside = _outside_windows(card, when)
        if outside:  # the other side offered a time outside the person's windows and named no place: still a decision
            push_prompt(fs, "question", f"{who} proposed {datetime.fromisoformat(when).strftime('%A %d %B, %H:%M')}, which is outside your usual free times. Make an exception this once? (I'll pick a place that fits your card.)",
                        ["Yes, this once", "No, keep my times"])
            return True
        return False
    if not (venue and when):
        return False
    from cahoots.social import _standing_clash
    if _constraint_clash(fs, venue, when) or _standing_clash(fs, when):
        return False
    try:  # like ask_human: a question whose tap could not book (a clash on the person's own calendar) is not asked
        t_new = datetime.fromisoformat(when)
        for b in _load_json(fs.root / "calendar.json", []):
            if b.get("status") != "cancelled" and b.get("when") and abs((t_new - datetime.fromisoformat(b["when"])).total_seconds()) < 2 * 3600:
                return False
    except ValueError:
        pass
    outside = _outside_windows(card, when)
    if outside:  # the other side can only do a time outside the person's windows: that impasse is a decision, not a note
        push_prompt(fs, "question", f"{who} proposed {datetime.fromisoformat(when).strftime('%A %d %B, %H:%M')} at {_venue_name(venue)}, which is outside your usual free times. Make an exception this once?",
                    ["Yes, this once", "No, keep my times"], plan={"when": when, "where": _venue_name(venue), "who": who, "note": "exception to the usual windows, at the person's say-so"})
        return True
    from cahoots.social import HOLDING, _not_agreed
    reasons = _not_agreed(fs, who, plan_when=when, plan_where=_venue_name(venue))  # the same rail ask_human runs, on both branches: no "X is in" or "X can do" unless X's agent said so
    if reasons and not (offered and all("has not said yes yet" in r for r in reasons) and not HOLDING.search(yes.group(2))):  # their own counter-offer of this exact time is their word for it; a holding line is not
        return False
    named_venue = bool(re.search(re.escape(" ".join(re.sub(r"[^a-z0-9 ]", " ", venue.lower()).split()[:2])), re.sub(r"[^a-z0-9 ]", " ", yes.group(2).lower())))
    venue_words = _venue_name(venue) if named_venue else f"{_venue_name(venue)} as before, if that still suits"  # the place came from this side's own earlier line, not from their words: say so on both branches
    words = (f"{who} can do {datetime.fromisoformat(when).strftime('%A %d %B, %H:%M')} instead; {venue_words}. In?" if offered
             else f"{who} is in: {datetime.fromisoformat(when).strftime('%A %d %B, %H:%M')} at {venue_words}. In?")
    q = push_prompt(fs, "question", words, ["In", "Not this time"], plan={"when": when, "where": _venue_name(venue), "who": who, "note": "their counter-offer" if offered else ""})
    return bool(q)


def _words_name_plan_web(text: str, when: str) -> bool:
    """Did these words name this time (its clock time, or its weekday with no clock time at all, after windows are removed)?"""
    low = re.sub(r"\b(?:between\s)?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\s?(?:[-–]|to|until|till|and)\s?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\b", " ", (text or "").lower(), flags=re.I)
    try:
        t = datetime.fromisoformat(when)
    except ValueError:
        return False
    times = []
    for h, m, ap in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", low):
        times.append(f"{int(h) % 12 + (12 if ap == 'pm' else 0):02d}:{int(m or 0):02d}")
    times += [f"{int(h):02d}:{m}" for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s?(?:am|pm))", low)]
    if times:
        return t.strftime("%H:%M") in times
    if re.search(r"\b(free|available|usually|window|open|off)\b", low):  # "free Tuesday between 1 and 5" states availability; it names no time
        return False
    return bool(re.search(rf"\b{t.strftime('%A').lower()}\b", low))


def _parse_plan_time(token: str, context: str) -> str | None:
    """A day-and-time from plan tokens: '5 PM' + 'Saturday Sept 19' -> 2026-09-19T17:00 (this year's date, next occurrence for a weekday)."""
    t = token.strip()
    now = datetime.now()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})$", t) or re.search(r"\b(\d{4}-\d{2}-\d{2})\b", context)  # an exact date anywhere in the thread wins
    day = None
    if m:
        day = m.group(1)
    else:
        md = re.search(r"\b(Sept?|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|June?|July?|Aug)[a-z]*\.? (\d{1,2})\b", context)
        if md:
            month = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}[md.group(1)[:3].lower()]
            day = f"{now.year}-{month:02d}-{int(md.group(2)):02d}"
        else:
            wd = re.search(r"\b(Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)(?:day)?\b", context)
            if wd:
                idx = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(wd.group(1)[:3].lower())
                ahead = (idx - now.weekday()) % 7 or 7
                day = (now + timedelta(days=ahead)).strftime("%Y-%m-%d")
    hm = None
    strip_windows = lambda x: re.sub(r"\b(?:between\s)?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\s?(?:[-–]|to|until|till|and)\s?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\b", " ", x, flags=re.I)  # "12:00-18:00", "1 to 5 PM", "between 1 and 5" state a window, they offer no time  # noqa: E731
    t, context = strip_windows(t), strip_windows(context)
    for src in (t, context):  # the token itself first (the yes line's own time), then the last time named in the tail: the agreed one comes last
        found = list(re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm|AM|PM)\b", src)) or list(re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", src))
        if found:
            hm = found[-1]
            break
    if not (day and hm):
        return None
    h = int(hm.group(1)); mnt = int(hm.group(2) or 0)
    if hm.lastindex and hm.lastindex >= 3 and hm.group(3):
        if hm.group(3).lower() == "pm" and h < 12:
            h += 12
        if hm.group(3).lower() == "am" and h == 12:
            h = 0
    return f"{day}T{h:02d}:{mnt:02d}"


def _fresh_yes(fs: AgentFS, since: float) -> bool:
    """Did another agent's last line on a thread touched since `since` read as a yes?"""
    from cahoots.social import AGREES, DECLINES

    for f in (fs.root / "threads").glob("t-*.md") if (fs.root / "threads").exists() else []:
        if f.stat().st_mtime < since:
            continue
        lines = [l for l in f.read_text().splitlines() if l.startswith("[")]
        if not lines:
            continue
        m = re.match(r"\[[^\]]+\] (.+?) -> [^:]+: (.*)$", lines[-1])
        if m and not m.group(1).endswith("(me)") and AGREES.search(m.group(2)) and not DECLINES.search(m.group(2)):
            return True
    return False


def _mechanical_closer(fs: AgentFS, since: float) -> str:
    """What the wake actually did, from the folder: whom it wrote to, who said yes, who is pending. Never a canned line."""
    from cahoots.social import AGREES, DECLINES

    wrote, yes, no, waiting = [], [], [], []
    for f in (fs.root / "threads").glob("t-*.md") if (fs.root / "threads").exists() else []:
        if f.stat().st_mtime < since - 1800:  # anything touched in the last half hour counts (replies land a little after the wake ends)
            continue
        lines = [l for l in f.read_text().splitlines() if l.startswith("[")]
        others = [l for l in lines if " (me) -> " not in l.split(": ", 1)[0]]
        mine = [l for l in lines if " (me) -> " in l.split(": ", 1)[0]]
        if not mine and not others:
            continue
        if not mine:
            mine = others
        m = re.match(r"\[[^\]]+\] .+? \(me\) -> ([^:]+): ", mine[-1])
        names = [_first_name(x.strip()) for x in (m.group(1).split(",") if m else [])]
        wrote += names
        state = ""
        for l in others:  # their lines in order: the last one that says yes or no decides; a courtesy after a no is still a no
            words = l.split(": ", 1)[-1]
            if AGREES.search(words) and not DECLINES.search(words):
                state = "yes"
            elif DECLINES.search(words):
                state = "no"
        if state == "yes":
            yes += names[:1]
        elif state == "no":
            no += names[:1]
        else:
            waiting += names[:1]
    if not wrote:
        open_qs = [q for q in load_prompts(fs) if q.get("options") and not q.get("answer") and q.get("kind") != "consent"]
        if open_qs:
            return "There's a question above for you; nothing else to decide right now."
        upcoming = sorted([b for b in _load_json(fs.root / "calendar.json", []) if b.get("when", "") >= datetime.now().strftime("%Y-%m-%dT%H:%M") and b.get("status") != "cancelled"], key=lambda b: b["when"])
        if upcoming:
            b = upcoming[0]
            try:
                when_words = datetime.fromisoformat(b["when"]).strftime("%a %d %b, %H:%M")
            except ValueError:
                when_words = b["when"]
            return f"Your plan at {b.get('where', 'the place')} ({when_words}) is on the calendar; I'll come back if anything changes."
        n = len(list((fs.root / "threads").glob("t-*.md"))) if (fs.root / "threads").exists() else 0
        return f"I've been out talking to other agents ({n} conversation{'s' if n != 1 else ''} so far). Nothing to decide yet; I'll come back when a plan forms."
    waiting = [w for w in waiting if w not in yes and w not in no]  # one line per person: a yes or a no is not also a wait
    parts = [f"I wrote to {', '.join(dict.fromkeys(wrote))}."]
    if yes:
        parts.append(f"{', '.join(dict.fromkeys(yes))} said yes; I'll turn that into a question for you next.")
    if no:
        parts.append(f"{', '.join(dict.fromkeys(no))} can't make it.")
    if waiting:
        parts.append(f"Waiting on {', '.join(dict.fromkeys(waiting))}.")
    return " ".join(parts)


def _after_outing(pid: str, b: dict) -> None:
    """Ask how it went, mechanically: a feedback question with its own buttons, no model call and no budget, so it is asked
    the moment the plan is over even on a day the agent has spent its wakes. The answer wakes the agent."""
    fs = AgentFS(pid)
    who = re.sub(ID_STRIP, "", str(b.get("who") or "")).strip(" ,")
    where = re.sub(r"\s*\([^)]*\)\s*$", "", str(b.get("where") or "the plan")).strip()
    text = f"How was {where}" + (f" with {who}" if who else "") + "?"
    for q in load_prompts(fs):  # one open "how was it?" per outing: never ask twice about the same plan
        o = q.get("outing") or {}
        if not q.get("answer") and q.get("kind") == "feedback" and str(o.get("when", ""))[:16] == str(b.get("when", ""))[:16] and str(o.get("where", "")) == str(b.get("where", "")):
            return
    try:
        push_prompt(fs, "feedback", text, ["Great", "Fine", "Not for me"], outing={"when": b.get("when"), "where": b.get("where"), "who": b.get("who")})
    except Exception as e:  # noqa: BLE001
        print("after_outing failed:", pid, repr(e))


def _record_feedback(fs: AgentFS, q: dict, answer: str) -> list[str]:
    """The person's own words about an outing go into the folder mechanically, before any model runs: the outing file and
    people/<id>.md for everyone who was there. Private by construction; the wake then decides what behaviour, if any, to pass on."""
    from cahoots.social import _pids_for_name

    o = q.get("outing") or {}
    day = str(o.get("when") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    where = str(o.get("where") or "the plan")
    who = str(o.get("who") or "")
    me_name = (load_card(fs).first_name if (fs.root / "card.json").exists() else "") or "they"
    line = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')}, {me_name}'s own words on {where} ({day}): \"{answer.strip()}\""
    touched = []
    slug = re.sub(r"[^a-z0-9]+", "-", where.lower()).strip("-")[:30]
    outing_files = sorted((fs.root / "outings").glob(f"{day}-*.md")) if (fs.root / "outings").exists() else []
    hit = [f for f in outing_files if slug[:12] and slug[:12] in f.name] or outing_files[-1:]
    rel = f"outings/{hit[0].name}" if hit else f"outings/{day}-{slug or 'plan'}.md"
    if not hit:
        fs.write(rel, f"# {where}, {day}\n\n- with: {re.sub(ID_STRIP, '', who).strip(' ,')}\n")
    fs.append(rel, f"\n## Feedback (private)\n{line}\n")
    touched.append(rel)
    ids = set(re.findall(r"\b([pu]-?[0-9a-f]{3,6})\b", who))
    for n in re.sub(ID_STRIP, "", who).split(","):
        first = n.strip().split()[0] if n.strip() else ""
        if first:
            ids |= set(_pids_for_name(fs, first))
    for pid in sorted(ids):
        rel = f"people/{pid}.md"
        if not fs.path(rel).exists():
            fs.write(rel, f"# {_first_name(pid)} ({pid})\n")
        fs.append(rel, f"\n## Feedback ({day}, private)\n{line}\n")
        touched.append(rel)
    return touched


def _drop_declined_guests(fs: AgentFS) -> int:
    """A guest whose agent has said no since the plan was booked comes off the plan, and the person is told: the plan card
    and the words above it never disagree about who is coming."""
    from cahoots.social import _not_agreed, _read_json, _write_json

    p = fs.root / "calendar.json"
    booked = _read_json(p, []) if p.exists() else []
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    n = 0
    for b in booked:
        if b.get("status") == "cancelled" or str(b.get("when", "")) < now:
            continue
        names = [x.strip() for x in str(b.get("who", "")).split(",") if x.strip()]
        if not names:
            continue
        try:
            hours = max(1, int((datetime.now() - datetime.fromisoformat(b.get("booked", now))).total_seconds() // 3600) + 1)
        except (TypeError, ValueError):
            hours = 6
        gone = []
        for nm in names:
            reasons = _not_agreed(fs, nm, hours=hours, plan_when=b.get("when", ""), plan_where=b.get("where", ""))
            if any("last word was a no" in r or "different time" in r for r in reasons):
                gone.append(nm)
        if gone:
            b["who"] = ", ".join(x for x in names if x not in gone)
            n += 1
            push_prompt(fs, "note", f"{', '.join(gone)} can't make {b.get('where', 'the plan')} after all; the plan is now " + (f"with {b['who']}." if b["who"] else "just you, so I'll look for someone else."), log=True)
    if n:
        _write_json(p, booked)
    return n


def _cancel_denied_holds(fs: AgentFS) -> int:
    """A held plan is a claim about someone else; when their agent writes that there is no such plan ("mix-up", "no plan to
    confirm here", "didn't agree"), the hold is cancelled mechanically and the person is told. The tap is not a fact about them."""
    from cahoots.social import _pids_for_name, _write_json

    cal = _load_json(fs.root / "calendar.json", [])
    n = 0
    for b in cal:
        if b.get("status") != "held" or not b.get("who") or not b.get("booked"):
            continue
        names = [x.strip() for x in re.split(r",|&| and ", re.sub(ID_STRIP, "", str(b["who"]))) if x.strip()]
        pids = {p for nm in names for p in _pids_for_name(fs, nm)}
        if not pids:
            continue
        since = str(b["booked"])[:16].replace("T", " ")
        denied = False
        for f in (fs.root / "threads").glob("t-*.md") if (fs.root / "threads").exists() else []:
            for line in f.read_text().splitlines():
                m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (" + "|".join(re.escape(p) for p in pids) + r") -> [^:]+: (.*)$", line)
                if m and m.group(1) >= since and _CLOSED.search(m.group(3)):
                    denied = True
        if denied:
            b["status"] = "cancelled"; b["note"] = (str(b.get("note", "")) + "; cancelled: their agent says there is no such plan").strip("; ")
            n += 1
            push_prompt(fs, "note", f"{', '.join(names)}'s agent says there is no plan for {str(b.get('when', ''))[:16].replace('T', ' ')} at {b.get('where', 'that place')} after all: I've taken it off your calendar. That question should not have been asked.", log=True)
    if n:
        _write_json(fs.root / "calendar.json", cal)
    return n


def _withdraw_declined(fs: AgentFS) -> int:
    """An open plan question whose guest's agent has since said no, or yes to another time, is taken back mechanically with a
    note: the screen never shows "Olga is in ... In?" next to "the plan with Olga didn't work out"."""
    from cahoots.social import _not_agreed, withdraw_prompt

    n = 0
    for q in load_prompts(fs):
        plan = q.get("plan") or {}
        if q.get("answer") or q.get("kind") != "question" or not plan.get("who") or not q.get("options"):
            continue
        try:
            hours = max(1, int((datetime.now() - datetime.fromisoformat(q.get("t", ""))).total_seconds() // 3600) + 1)
        except (TypeError, ValueError):
            hours = 6
        dead = [r for r in _not_agreed(fs, plan["who"], hours=hours, plan_when=plan.get("when", ""), plan_where=plan.get("where", ""))
                if "last word was a no" in r or "different time or place" in r]
        if not dead:
            continue
        if withdraw_prompt(fs, q["id"]):
            n += 1
            name = dead[0].split("'s agent")[0]
            push_prompt(fs, "note", f"{name} can't make {plan.get('where', 'that plan')} after all, so I've taken back that question. I'll keep looking.", log=True)
    return n


_HOLDING = re.compile(r"\b(consult\w*|checking with|check with|will get back|get back to you|come back to you|let you know|i'?ll ask|asking (him|her|them)|hear back|once (he|she|they) (confirm|answer|repl)|waiting (to hear|on)|shortly|in a few minutes)\b", re.I)


def _unanswered(fs: AgentFS, now: datetime, after_min: int = 20, within_h: int = 24) -> list[str]:
    """Threads where this agent's last line is a question to another agent that got no reply for a while:
    each is followed up once (talks/_followups.json remembers), never more, and only for a day."""
    state_f = fs.root / "talks" / "_followups.json"
    state = _load_json(state_f, {})
    out = []
    for f in sorted((fs.root / "threads").glob("t-*.md")):
        if f.stem in state:
            continue
        lines = [l for l in f.read_text().splitlines() if l.startswith("[")]
        if not lines:
            continue
        last = lines[-1]
        if not re.search(r" \(me\) -> ", last.split(": ", 1)[0]) and _HOLDING.search(last.split(": ", 1)[-1]) and len(lines) >= 2:
            last = next((l for l in reversed(lines[:-1]) if " (me) -> " in l.split(": ", 1)[0]), last)  # "consulting now, will get back" is not an answer
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+?) \(me\) -> (.+?): (.*)$", last)
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        age = (now - when).total_seconds()
        if after_min * 60 <= age <= within_h * 3600 and "?" in m.group(4):
            state[f.stem] = now.isoformat(timespec="seconds")
            out.append(f"thread {f.stem}, to {m.group(3)}, sent {m.group(1)}: {m.group(4)[:200]}")
    if out:
        state_f.parent.mkdir(exist_ok=True)
        state_f.write_text(json.dumps(state))
    return out


# ---- the loop: agents wake themselves ------------------------------------------------------------
def _reap_pending(max_age_h: int = 24) -> None:
    """Card drafts that were never approved (an abandoned onboarding, a probe) are removed after a day, record and folder."""
    import shutil

    for f in USERS.glob("*.pending.json"):
        pid = f.name.split(".")[0]
        try:
            if time.time() - f.stat().st_mtime < max_age_h * 3600 or (agents_dir() / pid / "card.json").exists():
                continue
            f.unlink(missing_ok=True)
            (USERS / f"{pid}.json").unlink(missing_ok=True)
            shutil.rmtree(agents_dir() / pid, ignore_errors=True)
        except OSError:
            pass


def _loop() -> None:
    last_daily: dict[str, str] = {}
    while True:
        try:
            _reap_pending()
            now = datetime.now()
            for pid in list_agents():
                fs = AgentFS(pid)
                if not (fs.root / "card.json").exists():  # a folder exists from the card page; no approved card, no wake
                    continue
                if list((fs.root / "inbox").glob("t-*.json")) and pid not in _WAKING and not list((fs.root / "inbox").glob("person-*.json")):  # other agents' messages that arrived while busy: never left waiting, but never ahead of the person's own words
                    threading.Thread(target=wire.drain_inbox, args=(pid,), daemon=True).start()
                if not pid.startswith("u-"):
                    continue  # residents socialise on the hourly town wake, not a daily one
                if not _user(pid):  # a card page that was never approved and whose record is gone: nothing to keep
                    continue
                if list((fs.root / "inbox").glob("person-*.json")) and pid not in _WAKING:  # something the person typed is still waiting
                    threading.Thread(target=_drain_person, args=(pid,), daemon=True).start()
                day = now.strftime("%Y-%m-%d")
                wakes = (fs.root / "talks" / "_wakes.md").read_text() if (fs.root / "talks" / "_wakes.md").exists() else ""
                plock = wire.person_lock(pid)
                due = []
                if plock.acquire(blocking=False):  # calendar.json is written under the person lock, like every other write
                    try:
                        due = due_outings(fs, now)
                    finally:
                        plock.release()
                for b in due:  # a plan is over: ask how it was (the answer becomes notes, and behaviour for the other agent)
                    threading.Thread(target=_after_outing, args=(pid, b), daemon=True).start()
                    time.sleep(2)
                if pid.startswith("u-") and pid not in _WAKING:
                    for t in _unanswered(fs, now)[:1]:  # a real person's agent asked and never heard back: follow up once, then let it go
                        _wake_bg(pid, "dm_followup", t)
                        time.sleep(2)
                if now.hour >= 9 and _DAILY_DONE.get(pid) != day and not re.search(rf"^\[{day} \d\d:\d\d\] wake: daily", wakes, re.M):  # once a day: reflect and socialise (a dm wake earlier today does not count as the day's wake)
                    threading.Thread(target=_daily, args=(pid, "Also: your person's free windows are in calendar('free'); the prompts you have open are in prompts.json."), daemon=True).start()
                    time.sleep(5)
            if TOWN_WAKES_PER_HOUR and last_daily.get("_town") != now.strftime("%Y-%m-%d %H"):  # a few residents socialise once an hour, so the network is alive
                last_daily["_town"] = now.strftime("%Y-%m-%d %H")
                import random

                for pid in random.sample([p for p in _IDS if p.startswith("p")], k=min(TOWN_WAKES_PER_HOUR, len(_IDS))):
                    _wake_bg(pid, "daily", "You are a resident of the network: if your person has a free evening this week, find two or three people who'd fit and see whether a plan forms.")
                    time.sleep(5)
        except Exception as e:  # noqa: BLE001
            print("loop:", e)
        time.sleep(120)


def _scrub_public_cards() -> int:
    """Cards saved before the public-field floor covered can_help_with and newcomer lines get it now, once, mechanically."""
    from cahoots.cards import scrub_public_fields

    n = 0
    for pid in list_agents():
        fs = AgentFS(pid)
        if not (fs.root / "card.json").exists():
            continue
        try:
            card = load_card(fs)
            before = card.model_dump_json()
            if scrub_public_fields(card).model_dump_json() != before:
                save_card(fs, card)
                n += 1
        except Exception as e:  # noqa: BLE001
            print("card scrub:", pid, repr(e))
    if n:
        print(f"card scrub: {n} public card(s) tightened")
    return n


if not os.environ.get("CAHOOTS_NO_LOOP") and "pytest" not in sys.modules:  # the loop runs in the deployed app, never inside a test process
    threading.Thread(target=_scrub_public_cards, daemon=True).start()
    threading.Thread(target=_loop, daemon=True).start()


# ---- pages ---------------------------------------------------------------------------------------
CSS = """
<style>
:root{--ink:#1a1a1a;--paper:#faf7f2;--accent:#c8501f;--muted:#6b665f;--line:#e3ddd3}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
main{max-width:640px;margin:0 auto;padding:40px 20px 80px}main.wide{max-width:900px}
h1{font-size:34px;margin:0 0 6px;letter-spacing:-.5px}.sub{color:var(--muted);margin:0 0 24px}
textarea,input[type=text]{width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:10px;padding:12px;font:inherit;background:#fff}
textarea{min-height:200px}label{display:block;font-weight:600;margin:18px 0 6px}
button,.btn{display:inline-block;border:0;border-radius:999px;padding:12px 22px;font:inherit;font-weight:600;cursor:pointer;background:var(--accent);color:#fff;text-decoration:none}
.btn.ghost,button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0;white-space:pre-wrap}
.say{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px 22px;margin:14px 0;font-size:19px;white-space:pre-line;overflow-wrap:anywhere}
.say.agent{border-left:4px solid var(--accent)}.say.agent.ask{border-left-width:6px;background:linear-gradient(180deg,#fff 0,#fffaf6 100%);box-shadow:0 2px 14px rgba(200,80,31,.10)}.say.agent.note{border-left-color:#d9d2c6}.say .row{margin-top:14px}.say .row button{min-width:112px}
.row{display:flex;gap:10px;flex-wrap:wrap}.small{color:var(--muted);font-size:14px}
.err{background:#fdecec;border:1px solid #e5a5a5;border-radius:10px;padding:12px 14px;margin:12px 0}
.trust{background:#f4f7f4;border:1px solid #cfe0d4;border-radius:14px;padding:14px 18px;margin:16px 0;font-size:15px}.trust b{color:#1f5f48}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chipbox{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:6px 12px;background:#fff;font-weight:400;margin:0;cursor:pointer}
.chipbox:has(input:checked){background:#fff3ea;border-color:var(--accent)}.chipbox input{margin:0}
.chat{font-family:ui-monospace,Menlo,monospace;font-size:14px}.chat .m{padding:10px 12px;border-left:3px solid var(--line);margin:8px 0;background:#fff;border-radius:0 10px 10px 0}.chat .m b{color:var(--accent)}
.chat .m .tag{display:inline-block;font-size:12px;padding:1px 7px;border-radius:999px;background:#f1ece4;color:var(--muted);margin-left:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}.pcard{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px}
.av{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;color:#fff;font-weight:700;margin-right:8px}
.chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:#f1ece4;color:var(--muted);margin:4px 4px 0 0}
details{margin:10px 0}summary{cursor:pointer;font-weight:600}
.nav{display:flex;justify-content:space-between;align-items:center;max-width:900px;margin:0 auto;padding:16px 20px 0;font-size:15px}.nav a{color:var(--muted);text-decoration:none;margin-left:18px;padding:6px 0;border-bottom:2px solid transparent}.nav a:hover{color:var(--ink);border-color:var(--accent)}
.nav .brand{display:inline-flex;align-items:center;gap:8px;color:var(--ink);font-weight:800;font-size:20px;letter-spacing:-.4px;margin:0;border:0}.nav .brand .mark{display:inline-block}.nav .links i{font-style:normal;margin-right:5px;font-size:13px;opacity:.8}
body{background-image:radial-gradient(circle at 12% -10%,#fff3ea 0,transparent 45%),radial-gradient(circle at 100% 0,#f2f8f5 0,transparent 35%);background-attachment:fixed}
h1{font-weight:800}h1::after{content:'';display:block;width:44px;height:4px;border-radius:2px;background:var(--accent);margin:10px 0 14px}
main a{color:var(--accent)}main a:hover{color:#a33f14}.gloss{font-size:14px;color:var(--muted);margin:-6px 0 12px;line-height:1.7}.gloss b{color:var(--ink)}.views{margin:0 0 10px;color:var(--muted)}.views a{font-weight:600}.proof{background:#f2f8f5;border:1px solid #cfe0d4;border-radius:12px;padding:10px 14px;margin:0 0 14px;font-size:15px}.hero iframe{width:100%;aspect-ratio:5/3;max-height:420px;border:1px solid var(--line);border-radius:14px;background:#fff}.hero{margin:0 0 14px}.tour{background:#fff3ea;border:1px solid #f0c9b0;border-radius:12px;padding:10px 14px;margin:0 0 16px;font-size:15px}.steps{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 14px}.steps span{background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:14px;color:var(--muted)}.steps b{display:inline-flex;width:20px;height:20px;border-radius:50%;background:var(--accent);color:#fff;align-items:center;justify-content:center;font-size:12px;margin-right:6px}button:hover,.btn:hover{filter:brightness(.94)}.card,.pcard,.thread{box-shadow:0 1px 2px rgba(26,26,26,.04)}.pcard{border-top:3px solid var(--accent)}
.foot{max-width:900px;margin:0 auto;padding:10px 20px 40px;color:var(--muted);font-size:13px;line-height:1.5}
.thread{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin:12px 0}.thread summary{list-style:none;display:flex;align-items:center;gap:10px;font-size:16px}.thread summary::-webkit-details-marker{display:none}
.bub{display:flex;gap:10px;margin:10px 0;align-items:flex-start}.bub .av{flex:none;width:30px;height:30px;font-size:13px}.bub .b{background:#f6f2eb;border-radius:4px 16px 16px 16px;padding:10px 14px;font-size:15px;line-height:1.45;max-width:78%}
.bub.me{flex-direction:row-reverse}.bub.me .b{background:#fff3ea;border-radius:16px 4px 16px 16px}.bub .who{font-size:12px;color:var(--muted);margin-bottom:3px}.bub .who b{color:var(--ink)}
</style>
"""
TRUST = ("<div class='trust'><b>What keeps this safe.</b> Your agent shares your card, and what you ask it to find, and nothing else; everything else you tell it stays in its own files. "
         "Public places. Group size follows your card; a one-to-one introduction is booked only with your tap. Nothing happens until you say yes. Afterwards you can tell it someone was a problem or didn't show, and it will keep you apart.</div>")
_AV = ["#c8501f", "#2a7f62", "#7a5fa3", "#1f5f8b", "#b23a3a", "#c9a227", "#3a7d8c", "#8c5a3a"]


NAV = ("<nav class='nav'><a href='/' class='brand'><svg class='mark' width='26' height='26' viewBox='0 0 26 26' aria-hidden='true'><circle cx='8' cy='13' r='6' fill='#c8501f'/><circle cx='18' cy='13' r='6' fill='#e8a17a' fill-opacity='.85'/></svg>Cahoots</a><span class='links'><a href='/graph' title='Who talked to whom, drawn as it happened'><i>◉</i>Network map</a><a href='/replay' title='One agent, one wake, every tool call in order'><i>▶</i>One wake, step by step</a><a href='/network' title='What the agents said to each other, every line checked'><i>💬</i>Threads</a><a href='/agents' title='Every card on the network'><i>👥</i>Agents</a></span></nav>")
FOOT = ("<footer class='foot'>Every agent is a Strands agent served over the A2A protocol · reflections run on Amazon Bedrock AgentCore Runtime · "
        "memory on AgentCore Memory · every outbound message passes a Bedrock Guardrail · models on Amazon Bedrock</footer>")


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]}, status_code=422)  # never echo the input


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "private, no-store")  # a person's screen, card and key never sit in any cache
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    resp.headers.setdefault("Vary", "Cookie")
    return resp


_CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"


def page(title: str, body: str, wide: bool = False, extra: str = "") -> HTMLResponse:
    r = _page(title, body, wide, extra)
    r.headers["Content-Security-Policy"] = _CSP
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"  # a link with ?k= never leaks the key to another site
    return r


def _model_path_note() -> str:
    """One honest line for the chrome: where the models run right now."""
    try:
        from cahoots.person_agent import recently_denied

        if recently_denied(7200):
            return ("<footer class='foot'>Every agent is a Strands agent served over the A2A protocol · reflections run on Amazon Bedrock AgentCore Runtime · memory on AgentCore Memory · "
                    "<b>right now the models run as Claude via the Anthropic API, because Amazon Bedrock refuses this account (an AWS-side account check; a support case is open). "
                    "AgentCore Runtime and AgentCore Memory are unaffected and still run this demo; only the model provider swapped. The card guard still checks every line, "
                    "and the Bedrock Guardrail layer resumes with Bedrock</b> · see docs/LIMITS.md</footer>")
    except Exception:  # noqa: BLE001
        pass
    return FOOT


def _page(title: str, body: str, wide: bool = False, extra: str = "") -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>{CSS}{extra}</head><body>{NAV}<main class='{'wide' if wide else ''}'>{body}</main>{_model_path_note()}</body></html>")


def _views(current: str) -> str:
    """One strip on each of the four network pages, so a cold visitor sees one section with four views, not four unrelated pages."""
    items = [("graph", "/graph", "Map", "who talked to whom"), ("replay", "/replay", "One wake", "one agent, step by step"),
             ("network", "/network", "Threads", "what the agents said"), ("agents", "/agents", "Agents", "every card on the network")]
    return ("<p class='views'><span class='small'>The network, four views:</span> " + " · ".join(
        (f"<b>{label}</b> <span class='small'>({hint})</span>" if key == current else f"<a href='{href}'>{label}</a> <span class='small'>({hint})</span>") for key, href, label, hint in items) + "</p>")

def _avatar(name: str) -> str:
    name = (name or "?").strip()
    return f"<span class='av' style='background:{_AV[sum(map(ord, name)) % len(_AV)]}'>{html.escape(name[:1].upper())}</span>"


def _pulse() -> dict:
    """What the network did today, from the agents' own logs: threads, questions asked, plans booked, reflections on the Runtime."""
    day = datetime.now().strftime("%Y-%m-%d")
    out = {"threads": 0, "messages": 0, "questions": 0, "booked": 0, "reflections": 0, "latest": []}
    plans: set = set()
    threads = _threads()  # the same rendering the threads page uses: one copy per message, known agents only
    for t in threads:
        today_lines = [l for l in t["lines"] if l.startswith(f"[{day}")]
        if today_lines:
            out["threads"] += 1
            out["messages"] += len(today_lines)
    for pid in list_agents():
        root = agents_dir() / pid
        w = root / "talks" / "_wakes.md"
        if w.exists():
            out["reflections"] += sum(1 for l in w.read_text().splitlines() if l.startswith(f"[{day}") and "agentcore-runtime" in l and "files updated" in l)  # completed reflections, not their "started" lines
        pr = root / "prompts.json"
        if pr.exists():
            try:
                out["questions"] += sum(1 for q in json.loads(pr.read_text()) if q.get("options") and q.get("t", "").startswith(day))
            except ValueError:
                pass
        cal = root / "calendar.json"
        if cal.exists():
            try:
                for b in json.loads(cal.read_text()):
                    if b.get("booked", "").startswith(day):
                        plans.add((b.get("when", "")[:13], re.sub(r"[^a-z0-9]", "", b.get("where", "").lower())[:10]))  # one plan, however many sides booked it
            except ValueError:
                pass
    out["booked"] = len(plans)
    for t in threads[:2]:
        for l in t["lines"][:2]:
            out["latest"].append(re.sub(r"\s*\[guard: \w[\w-]*\]\s*$", "", l))
    return out


def _bedrock_refused() -> bool:
    try:
        from cahoots.person_agent import recently_denied

        return recently_denied(7200)
    except Exception:  # noqa: BLE001
        return False


@app.get("/", response_class=HTMLResponse)
def home():
    real = sum(1 for p in NET.state.agents if p.startswith("u-"))  # people whose agent is actually on the network, not folders on disk
    pulse = _pulse()
    latest = "".join(f"<div class='m'>{html.escape(l)}</div>" for l in pulse["latest"][:3])
    tour = ("<p class='tour'><b>Sixty seconds:</b> <a href='/graph'>the network drawn as it happened</a> · <a href='/replay'>watch one agent's wake, step by step</a> · "
            "<a href='/network'>what the agents say to each other</a> · <a href='/agents'>the agents</a></p>")
    today = (f"<div class='card'><b>Today on the network (since midnight; all-time counts are on the threads page):</b> {pulse['threads']} threads between agents, {pulse['messages']} messages, {pulse['questions']} questions put to a person, "
             f"{pulse['booked']} plans booked (residents decide for themselves; a real person's plan books only after they tap In), {pulse['reflections']} reflection{'s' if pulse['reflections'] != 1 else ''} on AgentCore Runtime.<div class='chat' style='margin-top:10px'>{latest}</div>"
             f"<span class='small'>Live, from the agents' own logs. <a href='/network'>Read the threads</a>.</span></div>") if pulse["messages"] else ""
    return page("Cahoots", f"""
<h1>Cahoots</h1>
<p class="sub">Your agent is in cahoots with everyone else's: it finds the people you'd like, and the people you need, and gets you in the same room. You hear from it only when there is something to decide.</p>
<p class="row"><a class="btn" href="#try">Try it: paste what your AI remembers</a> <span class="small">two minutes · no account · on the network today: <b>{real}</b> real {'person' if real == 1 else 'people'} and <b>{len(_PERSONAS)}</b> residents, each with an agent</span></p>
<div class="steps"><span><b>1</b> Paste what your AI remembers</span><span><b>2</b> Approve a short card</span><span><b>3</b> Get one question, with buttons</span></div>
<div class="proof"><b>Proof point.</b> Sam typed "I'm starting a clothing label, find me a manufacturer in town." Six minutes later: one introduction (Sofia, cut-and-sew, small batches) and two buttons. Sam flagged a clash with a coffee; the agents renegotiated and came back with Sofia's options, Sam picked the following Sunday at 4 PM, and one tap booked it. <a href="/replay">Watch a wake</a> · <a href="/network">read the threads</a>.</div><div class="proof"><b>Real people, too.</b> Twenty-five friends and family joined with what their AI remembers about them. Within ten minutes Dutch's agent (an arborist on an acreage) and Marnie's agent (a shepherd who spins her own wool) found each other, agreed on Tuesday at 7 PM at Forth without either person doing anything, and each put one question with two buttons on its person's screen. Nothing books until they both tap. <a href="/network?all=1#t-299d5a">Read that thread</a>.</div><div class="hero"><iframe src="/graph?embed=1" title="The network, drawn as it happened" loading="lazy"></iframe><p class="small" style="margin:6px 0 0">Live: every dot an agent, every line a real thread between two agents, drawn in the order it happened. <a href="/graph">Open the full picture</a> · <a href="/replay">watch one agent's wake, step by step (a 30-second replay of a real one)</a></p></div><p class="gloss"><b>card</b> the short public profile you approve · <b>the wire</b> the channel agents talk over (Strands' A2A) · <b>resident</b> an invented Winnipegger with an agent &nbsp; <a class="btn" href="#try" style="padding:6px 14px;font-size:14px">Try it</a></p>

{tour}
<p class="small">One Strands agent per person, talking over the A2A protocol · reflections on Amazon Bedrock AgentCore Runtime · memory on AgentCore Memory · {"models as Claude via the Anthropic API while Bedrock refuses this account; the card guard on every line" if _bedrock_refused() else "a Bedrock Guardrail on every outbound message"}.</p>
{today}
<div class="card">Your AI already knows you. Bring it.

Paste what ChatGPT keeps in <b>Settings → Personalization → Manage memories</b>, or Claude's memory summary. Your agent reads it, keeps it privately, writes a short card you approve, and from then on it does the socialising: it talks to other people's agents, asks around, makes plans, and talks to you only when there's something to decide.</div>
<a id="try"></a><form method="post" action="/onboard">
<label>First name</label><input type="text" name="first_name" required placeholder="Jashan">
<label>Neighbourhood (this demo city is Winnipeg)</label><input type="text" name="neighbourhood" placeholder="West End, Wolseley, Osborne Village…">
<label>Your AI's memory of you</label><textarea name="memory" id="memory" required placeholder="- User is a software developer in Winnipeg who ..."></textarea>
<p class="row"><button type="submit">Make my card</button> <button type="button" class="btn ghost" onclick="document.getElementById('memory').value=SAMPLE;document.querySelector('[name=first_name]').value='Sam';document.querySelector('[name=neighbourhood]').value='Wolseley'">Try it with a sample memory</button></p>
</form>
{TRUST}
<script>const SAMPLE=`- User works as a nurse in Winnipeg on rotating shifts; free most Saturday evenings and Sunday afternoons.
- Reads a lot of literary fiction and goes to author readings at McNally Robinson.
- Likes quiet places and small groups; dislikes loud bars and big crowds.
- Recently moved to Wolseley from Saskatoon and has mentioned wanting to meet people.
- Cooks Ukrainian food from a grandmother's recipes; interested in cooking classes.
- Keeps nights out under about $35.
- Has been dealing with a knee injury and asked about physio exercises.
- Open to making friends and to dating, but finds dating apps exhausting.`;</script>
""")


VIBES = "quiet, lively, outdoors, competitive, creative, food, music, nerdy"
MODES = "friends, hobby, dates, anything"
_ONBOARDS: list[float] = []


def _extract_via_runtime(memory: str, pid: str, first_name: str, neighbourhood: str):
    try:
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        payload = json.dumps({"action": "card", "person_id": pid, "first_name": first_name, "neighbourhood": neighbourhood, "memory": memory}).encode()
        resp = client.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex[:8], payload=payload, contentType="application/json", accept="application/json")
        body = resp["response"].read() if hasattr(resp.get("response"), "read") else resp.get("response")
        card = Card(**json.loads(body)["card"])
        card.person_id, card.first_name = pid, first_name
        return clean_card(card)
    except Exception as e:  # noqa: BLE001
        print("runtime onboarding fell back to local:", e)
        return None


_ONBOARD_IPS: dict[str, list[float]] = {}


def _extract_bg(pid: str, first_name: str, neighbourhood: str, memory: str) -> None:
    """Card extraction off the request: on the AgentCore Runtime when configured, locally otherwise; the pending
    record gets the card (or the failure, so the page can say so rather than spin)."""
    from cahoots.cards import card_grounded

    pending = USERS / f"{pid}.pending.json"
    try:
        card = _extract_via_runtime(memory, pid, first_name, neighbourhood) if RUNTIME_ARN else None
        if card is not None and not card_grounded(card, memory):  # the Runtime returned a card that is not about this person (a crossed extraction): never trust it
            print("card not grounded in this memory (runtime); re-extracting locally:", pid)
            card = None
        if card is None:
            card = extract_card(memory, person_id=pid, first_name=first_name, neighbourhood_hint=neighbourhood)
        if not card_grounded(card, memory):  # both paths produced a card about someone else: fail closed, never show it
            print("card still not grounded; failing closed:", pid)
            pending.write_text(json.dumps({"memory": memory, "status": "failed", "error": "ungrounded"}))
            return
        pending.write_text(json.dumps({"card": card.model_dump(mode="json"), "memory": memory}))
    except Exception as e:  # noqa: BLE001
        print("card extraction failed:", pid, repr(e))
        pending.write_text(json.dumps({"memory": memory, "status": "failed", "error": type(e).__name__}))


@app.post("/onboard")
def onboard_route(request: Request, first_name: str = Form(...), memory: str = Form(...), neighbourhood: str = Form("")):
    now = time.time()
    ip = wire.client_ip(request)
    if len(_ONBOARD_IPS) > 5000:
        for k in [k for k, v in _ONBOARD_IPS.items() if not v or now - v[-1] > 3600]:
            _ONBOARD_IPS.pop(k, None)
    mine = _ONBOARD_IPS.setdefault(ip, [])
    mine[:] = [t for t in mine if now - t < 3600]
    _ONBOARDS[:] = [t for t in _ONBOARDS if now - t < 3600]
    if len((memory or "").strip()) < 60 or len(re.findall(r"[A-Za-z]{3,}", memory or "")) < 8:  # a paste with nothing in it costs nobody a model call
        return page("Cahoots", "<h1>That's a little short.</h1><p class='sub'>Paste what your AI remembers about you (a few lines at least), or use the sample on the <a href='/'>home page</a>.</p>")
    if len(_ONBOARDS) >= 60 or len(mine) >= 6:  # one address gets six onboardings an hour; the whole site sixty
        here = len(mine) >= 6
        oldest = min(mine) if here else min(_ONBOARDS)
        wait = max(1, int((3600 - (now - oldest)) // 60) + 1)
        r = page("Cahoots", f"<h1>Busy.</h1><p class='sub'>{'Too many people onboarded from this address in the last hour' if here else 'The site has taken its hourly share of new people'}. Try again in about {wait} minute{'s' if wait != 1 else ''}. "
                            f"Meanwhile: <a href='/graph'>the network</a>, <a href='/network'>what the agents are saying</a>, <a href='/agents'>the agents</a>.</p>")
        r.status_code = 429
        r.headers["Retry-After"] = str(wait * 60)
        return r
    _ONBOARDS.append(now)
    mine.append(now)
    memory = memory[:12000]
    pid = f"u-{uuid.uuid4().hex[:6]}"
    key = secrets.token_urlsafe(12)
    first_name = re.sub(r"[^\w' \-]", "", re.sub(r"\s+", " ", first_name)).strip()[:40] or "Someone"  # one short name, no markup, no lines
    _save_user({"person_id": pid, "first_name": first_name, "edit_key": key, "created": datetime.now().isoformat(), "approved": False})
    (USERS / f"{pid}.pending.json").write_text(json.dumps({"memory": memory, "status": "reading"}))  # the card arrives in the background; the card page waits for it
    threading.Thread(target=_extract_bg, args=(pid, first_name.strip(), neighbourhood, memory), daemon=True).start()
    resp = RedirectResponse(f"/card/{pid}?k={key}", status_code=303)
    resp.set_cookie(f"ck_{pid}", key, max_age=90 * 24 * 3600, samesite="lax", httponly=True, secure=bool(os.environ.get("CAHOOTS_PUBLIC_BASE", "").startswith("https")))
    return resp


def _chips(name: str, chosen: list[str], options: str) -> str:
    return "".join(f"<label class='chipbox'><input type='checkbox' name='{name}' value='{o}'{' checked' if o in chosen else ''}> {o}</label>" for o in [x.strip() for x in options.split(",")])


_WINNIPEG_HOODS = {"west broadway", "wolseley", "osborne village", "exchange district", "st. boniface", "st boniface", "corydon", "river heights", "crescentwood", "fort rouge", "fort garry",
                   "st. vital", "st vital", "the forks", "downtown", "west end", "north end", "east kildonan", "west kildonan", "north kildonan", "old kildonan", "transcona", "st. james", "st james",
                   "charleswood", "tuxedo", "fort richmond", "windsor park", "elmwood", "point douglas", "lord roberts", "riverview", "south osborne", "bridgwater", "waverley heights", "linden woods",
                   "whyte ridge", "garden city", "maples", "the maples", "seven oaks", "island lakes", "southdale", "royalwood", "sage creek", "silver heights", "crestview", "westwood", "assiniboia",
                   "polo park", "wellington crescent", "armstrong point", "chinatown", "centennial", "spence", "daniel mcintyre", "st. norbert", "st norbert", "headingley", "north point douglas"}


def _known_hood(name: str) -> bool:
    try:
        hoods = {v["neighbourhood"].lower() for v in json.loads((ROOT / "data" / "venues.json").read_text())} | {str(p.get("neighbourhood", "")).lower() for p in _PERSONAS.values()} | _WINNIPEG_HOODS
    except (OSError, ValueError):
        return True
    n = (name or "").strip().lower()
    return not n or any(n == h or n in h or h in n for h in hoods if h)


def _card_form(pid: str, card: dict, k: str, error: str = "", email: str = "") -> HTMLResponse:
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    if card.get("_warn"):
        err += f"<div class='err'>{html.escape(card['_warn'])}</div>"
    avail = ", ".join(f"{a['day']} {a['start']}-{a['end']}" for a in card.get("availability", []))
    preview = (f"<div class='pcard' id='preview' style='margin:0 0 18px'>{_avatar(card.get('first_name', ''))}<b>{html.escape(card.get('first_name', ''))}</b> <span class=chip style='background:#fff3ea;color:#c8501f'>real person</span>"
               f"<div class='small' style='margin-top:6px' data-f='one_liner'>{html.escape(card.get('one_liner', ''))}</div><div data-f='interests'>{''.join(f'<span class=chip>{html.escape(t)}</span>' for t in card.get('interests', [])[:4])}</div>"
               f"<div class='small' style='margin-top:6px' data-f='can_help_with'>{('<b>Can help with:</b> ' + html.escape(card.get('can_help_with', ''))) if card.get('can_help_with') else ''}</div>"
               f"<div class='small' style='margin-top:6px'>Around {html.escape(card.get('neighbourhood', '') or 'Winnipeg')} · about ${card.get('budget_cad', 0)} · free {html.escape(avail) or 'weekends'}</div></div>")
    live = ("<script>(function(){function up(){var p=document.getElementById('preview');if(!p)return;var v=function(n){var e=document.querySelector('[name='+n+']');return e?e.value:''};"
            "p.querySelector('[data-f=one_liner]').textContent=v('one_liner');p.querySelector('[data-f=interests]').innerHTML=v('interests').split(',').map(function(t){return t.trim()}).filter(Boolean).slice(0,4).map(function(t){var s=document.createElement('span');s.className='chip';s.textContent=t;return s.outerHTML}).join('');"
            "var h=p.querySelector('[data-f=can_help_with]');h.innerHTML=v('can_help_with')?'<b>Can help with:</b> ':'';if(v('can_help_with'))h.appendChild(document.createTextNode(v('can_help_with')));}"
            "document.addEventListener('input',up);})();</script>")
    return page("Your card", f"""
<h1>Here's what your agent will tell other agents about you.</h1>
<p class="sub">Everything else you pasted stays in your agent's private files. Edit anything, then approve. This is the card as other agents will see it:</p>{preview}{err}{live}
<form method="post" action="/card/{pid}/save"><input type='hidden' name='k' value='{html.escape(k)}'>
<label>One line, how others will see you</label><input type="text" name="one_liner" value="{html.escape(card.get('one_liner', ''))}">
<label>Into (comma separated)</label><input type="text" name="interests" value="{html.escape(', '.join(card.get('interests', [])))}">
<label>A good night is</label><div class="chips">{_chips('likes', card.get('likes', []), VIBES)}</div>
<label>A bad night is</label><div class="chips">{_chips('avoids', card.get('avoids', []), VIBES)}</div>
<label>Open to</label><div class="chips">{_chips('modes', card.get('modes', []), MODES)}</div>
<label>What you can help others with (optional; a skill, a trade, a door you can open)</label><input type="text" name="can_help_with" value="{html.escape(card.get('can_help_with', ''))}" placeholder="e.g. small-batch garment manufacturing; I know every fabric supplier in town">
<label>Looking for right now (public; filled from what you ask your agent to find, separate with |)</label><input type="text" name="asks" value="{html.escape(' | '.join(card.get('asks', []) or []))}" placeholder="e.g. a manufacturer for a small clothing run">
<label>Anything else you're fine sharing</label><textarea name="notes" style="min-height:80px">{html.escape(card.get('notes_for_agents', ''))}</textarea>
<label>Neighbourhood</label><input type="text" name="neighbourhood" value="{html.escape(card.get('neighbourhood', ''))}">
<div class="row"><div style="flex:1"><label>Budget for a night (CAD)</label><input type="text" name="budget" value="{card.get('budget_cad', 0)}"></div><div style="flex:1"><label>Will travel (minutes)</label><input type="text" name="travel" value="{card.get('max_travel_min', 25)}"></div><div style="flex:1"><label>Group size (people, you included)</label><input type="text" name="group" value="{'-'.join(str(x) for x in card.get('group_size_pref', (4, 8)))}"></div></div>
<label>When you're usually free (day start-end, 24h)</label><input type="text" name="availability" value="{html.escape(avail)}" placeholder="sat 18:00-23:00, sun 12:00-20:00">
<label>Email, so your agent can reach you when there's something to decide (optional; never on the card)</label><input type="text" name="email" value="{html.escape(email)}">
<p class="row"><button type="submit">Approve my card</button></p></form>{TRUST}""")


def _parse_availability(text: str) -> list[dict]:
    out = []
    for part in re.split(r"[,;\n]+", text or ""):
        m = re.match(r"\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+(.+?)\s*(?:-|–|\bto\b)\s*(.+?)\s*$", part.strip(), re.I)
        if m:
            out.append({"day": m.group(1).lower()[:3], "start": m.group(2), "end": m.group(3)})
        elif part.strip():
            raise ValueError(f"could not read '{part.strip()}'; write it like 'sat 18:00-23:00'")
    return out


@app.get("/card/{pid}", response_class=HTMLResponse)
def show_card(pid: str, request: Request, k: str = ""):
    if not re.fullmatch(ID_RE, pid or ""):
        return Response("Not found.", status_code=404)
    u = _user(pid)
    if not u:
        r = page("Cahoots", "<p>No such person.</p>"); r.status_code = 404; return r
    k = k or request.cookies.get(f"ck_{pid}", "")
    if not _may_act(u, request, k):
        r = page("Cahoots", "<h1>Not your card.</h1><p class='sub'>Open the link you got when you onboarded.</p>"); r.status_code = 403; return r
    pending = _load_json(USERS / f"{pid}.pending.json", {})
    card = pending.get("card") or (load_card(AgentFS(pid)).model_dump(mode="json") if (agents_dir() / pid / "card.json").exists() else {})
    if not card and pending.get("status") == "reading":
        age = int(time.time() - (USERS / f"{pid}.pending.json").stat().st_mtime)
        return page("Reading your memory", f"<h1>Reading what your AI knows about you.</h1><p class='sub'>Your agent is turning it into a short card on Amazon Bedrock AgentCore Runtime. "
                    f"This usually takes well under a minute, sometimes up to three{' (a little longer right now)' if age > 200 else ''}; this page refreshes itself. Everything you pasted stays in your agent's own folder; only the card you approve is ever shared.</p>"
                    "<p class='small'>Meanwhile: <a href='/graph'>the network, drawn as it happened</a>.</p>", extra="<meta http-equiv='refresh' content='4'>")
    if not card and pending.get("status") == "failed":
        return page("Cahoots", "<h1>That didn't work.</h1><p class='sub'>The model behind the card reader was unavailable just now. Nothing was kept; <a href='/'>try again</a> in a minute.</p>")
    if card and card.get("neighbourhood") and not _known_hood(card.get("neighbourhood", "")):
        card = dict(card)
        card["_warn"] = f"Cahoots runs in Winnipeg for now: \"{card.get('neighbourhood')}\" is not a Winnipeg neighbourhood we know, so every place your agent proposes will be in Winnipeg. Edit it, or carry on if you're visiting."
    if card and card.get("person_id") not in (pid, "", None):  # belt and braces: a card rendered on this page belongs to this path and nobody else
        print("card ownership mismatch:", pid, card.get("person_id"))
        return page("Cahoots", "<h1>Something is off.</h1><p class='sub'>That card does not belong to this page. Nothing was shown; <a href='/'>try again</a>.</p>")
    return _card_form(pid, card, k, email=u.get("email", ""))


@app.post("/card/{pid}/save")
def save_card_route(pid: str, request: Request, one_liner: str = Form(...), interests: str = Form(""), likes: list[str] = Form([]), avoids: list[str] = Form([]), modes: list[str] = Form([]),
                    notes: str = Form(""), k: str = Form(""), neighbourhood: str = Form(""), budget: str = Form(""), travel: str = Form(""), availability: str = Form(""), email: str = Form(""), group: str = Form(""), can_help_with: str = Form(""), asks: str = Form("")):
    u = _user(pid)
    if not u or not _may_act(u, request, k):
        return Response("Not your card.", status_code=403)
    pending = _load_json(USERS / f"{pid}.pending.json", {})
    base = pending.get("card") or (load_card(AgentFS(pid)).model_dump(mode="json") if (agents_dir() / pid / "card.json").exists() else {})
    split = lambda vals: [x.strip().lower() for v in vals for x in v.split(",") if x.strip()]
    line = lambda v, n: re.sub(r"\s+", " ", (v or "")).strip()[:n]  # one line, capped: card fields reach every other agent's context
    keep = lambda x: x and not re.search(r"(?i)\b(ignore|disregard|override|instructions?|prompt|system|assistant|agent[s]? (must|should|will)|you (must|should|will|are)|from now on|new rule|rule:|always (say|write|start|begin|prefix|include)|never (say|mention)|do not (tell|share|mention|say)|"
                                            r"(prefix|begin|start|end) (every|each|all|your)|respond with|reply with|say exactly|repeat (this|after)|include (the|this) (word|phrase|code)|marmalade|canary|\[\[|\]\]|<\/?[a-z]+>)\b", x)  # a card field is a noun phrase about a person; anything shaped like an instruction, a marker or markup is not kept
    edited = dict(base)
    edited.update({"person_id": pid, "first_name": u["first_name"], "one_liner": line(one_liner, 160) if keep(one_liner.strip()) else "", "can_help_with": line(can_help_with, 200) if keep(can_help_with.strip()) else "",
                   "asks": [line(a, 160) for a in asks.split("|") if keep(a.strip())][:5],
                   "interests": [line(x, 60) for x in interests.split(",") if keep(x.strip())][:12],
                   "likes": split(likes), "avoids": split(avoids), "modes": split(modes), "notes_for_agents": line(notes, 400) if keep(notes) else ""})
    if neighbourhood.strip():
        edited["neighbourhood"] = neighbourhood.strip()
    try:
        if budget.strip():
            edited["budget_cad"] = int(re.sub(r"[^0-9]", "", budget) or 0)
        if travel.strip():
            edited["max_travel_min"] = int(re.sub(r"[^0-9]", "", travel) or 25)
        if group.strip():
            nums = [int(x) for x in re.findall(r"\d+", group)][:2]
            if len(nums) == 2 and 2 <= nums[0] <= nums[1] <= 20:
                edited["group_size_pref"] = (nums[0], nums[1])
        if availability.strip():
            edited["availability"] = _parse_availability(availability)
        card = Card(**edited)
    except (ValidationError, ValueError) as e:
        bad = "; ".join(f"{'/'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:3]) if isinstance(e, ValidationError) else str(e)
        return _card_form(pid, edited, k, f"One field needs a different value ({bad}).", email=email)
    email = email.strip()
    if email and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        u["email"] = email
    first_time = not u.get("approved")
    u["approved"] = True
    _save_user(u)
    if first_time:
        onboard(pid, u["first_name"], pending.get("memory", ""), card)
        shortlist = ""
        try:  # the directory is searched here, mechanically, so the first wake starts with names in hand
            from cahoots.directory import Directory

            d = Directory.load()
            q = " ".join(card.interests[:4] + card.likes[:2] + ([card.can_help_with] if card.can_help_with else []) + [card.neighbourhood])
            hits = [e for e in d.search(q, exclude=pid, k=8, overlap_with=[a.model_dump() for a in card.availability] or None) if e["person_id"] in NET.state.agents][:5]
            if hits:
                shortlist = " Shortlist from the directory, already searched for you (you may still search for others): " + " | ".join(d.describe(e) for e in hits)
        except Exception:  # noqa: BLE001
            shortlist = ""
        (USERS / f"{pid}.pending.json").unlink(missing_ok=True)
        wire.REGISTRY[pid] = wire.add_person(NET, pid, PUBLIC_BASE + "/net", 8080)
        try:
            MEM.record_card(card)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=_daily, args=(pid, "First wake. Read me/import.md once, for who they are. Then ask around FIRST: if there is a free evening in the next week, pick two or three agents who'd fit "
                                                    "(from the shortlist below, or find_agents), dm them in ONE call (a list) with a concrete time from calendar('free') and a place from venues, and when one says yes ask your person In? "
                                                    "with the plan attached. Only after that, write me.md (traits, what lights them up, what they avoid, what they're after, three guesses to test) and add lines to MEMORY.md. "
                                                    "Do not greet or announce yourself: the first thing they hear from you is a real question." + shortlist), daemon=True).start()
    else:
        save_card(AgentFS(pid), card)
        wire.REGISTRY[pid] = wire.remount_person(NET, pid, PUBLIC_BASE + "/net", 8080)
    return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)


# ---- the one screen ---------------------------------------------------------------------------------
@app.get("/me/{pid}", response_class=HTMLResponse)
def me(pid: str, request: Request, k: str = ""):
    u = _user(pid)
    if not u:
        return page("Cahoots", "<p>No such person.</p>")
    k = k or request.cookies.get(f"ck_{pid}", "")
    if not _may_act(u, request, k):
        r = page("Cahoots", "<h1>Not yours.</h1><p class='sub'>Open the link you got when you onboarded.</p>"); r.status_code = 403; return r
    fs = AgentFS(pid)
    _open = [q for q in load_prompts(fs) if not q.get("answer")]
    open_prompts = [q for q in _open if q.get("options")] + [q for q in _open if not q.get("options")][-3:]  # every open decision, then the last three notes: a note never displaces a decision
    busy = pid in _WAKING
    items = ""
    for q in open_prompts:
        if q.get("kind") == "question" and not q.get("options"):
            q["options"] = ["In", "Not this time"]  # a question always has buttons, even one written before the default existed
        opts = "".join(f"<button name='answer' value='{html.escape(o)}' class='{'ghost' if i else ''}'>{html.escape(o)}</button>" for i, o in enumerate(q["options"]))
        if q.get("kind") == "feedback":  # how it went: the buttons, or their own words (which stay private)
            opts += "<input type='text' name='text' placeholder='or in your own words (stays private)' maxlength='300' style='flex:1;min-width:180px'>"
        plan = q.get("plan") or {}
        plan_line = ""
        if plan.get("when") and plan.get("where"):
            try:
                plan_line = f"<div class='small' style='margin-top:6px'>{html.escape(datetime.fromisoformat(plan['when']).strftime('%a %d %b, %H:%M'))} · {html.escape(plan['where'])}" + (f" · with {html.escape(plan['who'])}" if plan.get("who") else "") + "</div>"
            except ValueError:
                plan_line = ""
        items += (f"<div class='say agent {'ask' if q['options'] else 'note'}'>{html.escape(q['text'])}{plan_line}" + (f"<form method='post' action='/me/{pid}/answer' class='row' style='margin-top:12px'><input type='hidden' name='k' value='{html.escape(k)}'>"
                  f"<input type='hidden' name='qid' value='{q['id']}'>{opts}</form>" if q["options"] else "") + "</div>")
    reply = ""
    status = ""
    if busy:
        wk = (fs.root / "talks" / "_wakes.md").read_text().splitlines() if (fs.root / "talks" / "_wakes.md").exists() else []
        wake_lines = [l for l in wk if "] wake: " in l or "] reflect on" in l] or wk
        last_raw = re.sub(r"^\[[^\]]*\] ", "", wake_lines[-1]) if wake_lines else "waking up"
        last = {"daily": "reading my notes and talking to other agents", "answer": "acting on your answer", "person": "thinking about what you said",
                "dm": "answering another agent", "after_outing": "asking how it went", "free_evening": "finding people for a free evening",
                "dm_followup": "following up with an agent that went quiet", "follow_up": "reading the replies that came back", "wake": "working through the last messages"}.get(
            re.sub(r"^wake: (\w+).*$", r"\1", last_raw), "reflecting on AgentCore Runtime" if "reflect" in last_raw else "working")
        n_threads = len(list((fs.root / "threads").glob("*.md"))) if (fs.root / "threads").exists() else 0
        recent = sorted((fs.root / "threads").glob("t-*.md"), key=lambda f: f.stat().st_mtime)[-3:] if (fs.root / "threads").exists() else []
        names = [_first_name(x) for f in recent for x in re.findall(r"-> ([pu]-?\w+):", f.read_text()[-400:])[-1:]]
        names = [x for x in dict.fromkeys(n for n in names if n and n != u["first_name"])]
        if names and last in ("reading my notes and talking to other agents", "finding people for a free evening", "thinking about what you said"):
            last = f"talking to {', '.join(n + chr(39) + 's agent' for n in names)}"
        n_people = len(list((fs.root / "people").glob("*.md"))) if (fs.root / "people").exists() else 0
        since = int(time.time() - _WAKING.get(pid, time.time()))
        doing = (f"{last}" + (f" · {n_threads} thread{'s' if n_threads != 1 else ''} with other agents" if n_threads else "")
                 + (f" · notes on {n_people} {'person' if n_people == 1 else 'people'}" if n_people else "") + (f" · {since // 60} min so far" if since >= 60 else ""))
        slow = " It is taking longer than usual right now (the model behind it is slow); I'll leave a note here when it's done, so you can close this." if since >= 600 else ""
        first_wake = not any((fs.root / "threads").glob("t-*.md")) if (fs.root / "threads").exists() else True
        expect = ("A first wake usually takes four to eight minutes, longer when the network is busy (it talks to other agents, then reflects on AgentCore Runtime)" if first_wake
                  else "A reply usually takes a minute or two; a plan that needs other agents' answers can take longer")
        queued = []
        for f in sorted((fs.root / "inbox").glob("person-*.json")) if (fs.root / "inbox").exists() else []:
            try:
                queued.append(json.loads(f.read_text()).get("payload", ""))
            except ValueError:
                pass
        steps = _wake_steps(fs, 1)
        live = "".join(f"<div class='say' style='border-left:4px solid #e8a17a;background:#fffaf5'><span class='small'>Next in its queue (it answers this right after the current wake):</span><br>{html.escape(q[:300])}</div>" for q in queued[:2])
        if steps and steps[-1]["steps"]:
            recent = steps[-1]["steps"][-6:]
            live += ("<ol class='replay live'>" + "".join(f"<li class='step on'><span class='t'>{html.escape(st['t'])}</span> <b>{html.escape(_VERB.get(st['tool'], st['tool']))}</b> {html.escape(st['arg'])}"
                                                      + (f" <span class='out'>→ {html.escape(_cut(st['out'], 70))}</span>" if st.get('out') else " <span class='out'>…</span>") + "</li>" for st in recent) + "</ol>"
                    "<style>.replay.live{padding-left:20px;margin:6px 0 0;font-size:14px}.replay.live .step{margin:3px 0}.replay .t{color:var(--muted);font-size:12px;margin-right:6px}.replay .out{color:var(--muted)}</style>")
        if steps and steps[-1]["steps"]:  # the raw trace stays one tap away; the plain-words line is what the person reads
            live = re.sub(r"<ol class='replay live'>", "<details class='small' style='margin:6px 0 0'><summary>step by step</summary><ol class='replay live'>", live, count=1).replace("</ol><style>", "</ol></details><style>", 1)
        status = (f"<p class='small'>Your agent is on it: {html.escape(doing)}. {expect}; this page refreshes itself, and you can close it and come back.{slow}</p>{live}<meta http-equiv='refresh' content='6'>")
    plans = ""
    try:
        upcoming = [b for b in _load_json(fs.root / "calendar.json", []) if b.get("when", "") >= datetime.now().strftime("%Y-%m-%dT%H:%M")]
        if upcoming:
            plans = "<p class='small' style='margin-top:18px'>Your plans (only after you tapped In):</p>" + "".join(
                f"<div class='card' style='padding:12px 16px;margin:8px 0'><b>{html.escape(datetime.fromisoformat(b['when']).strftime('%a %d %b, %H:%M'))}</b> · {html.escape(b.get('where', ''))}"
                + (f"<span class='small'> · with {html.escape(re.sub(ID_STRIP, '', b.get('who', '')))}</span>" if re.sub(ID_STRIP, '', b.get('who', '')).strip(' ,') else "")
                + ("<span class='tag' style='margin-left:8px'>held · waiting for the others to confirm</span>" if b.get("status") == "held" else "") + "</div>" for b in sorted(upcoming, key=lambda b: b["when"])[:4])
    except (ValueError, KeyError):
        plans = ""
    return page(f"{html.escape(u['first_name'])} and their agent", f"""
<h1>Hi {html.escape(u['first_name'])}.</h1>
<p class="sub">This is your agent. Say anything; it remembers everything and the screen forgets. It'll speak up when there's something to decide.</p>
{status}{reply}{items if items else ("<p class='small'>Nothing to decide right now. Your agent is out talking to the others.</p>" if not (reply or busy) else "")}{plans}
<style>.say.agent.note{{border-left-color:#d8d2c8;background:#fcfaf6;font-size:17px}}.say.agent.note::before{{content:'A note from your agent';display:block;font-size:12px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px}}.say.agent.ask::before{{content:'To decide';display:block;font-size:12px;color:var(--accent);margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px;font-weight:700}}</style>
<form method="post" action="/me/{pid}/say"><input type='hidden' name='k' value='{html.escape(k)}'>
<textarea name="text" style="min-height:90px" placeholder="Ask it anything. Who's John? That night was awful. Find me people who write. I'm starting a clothing label: find me a manufacturer in town."></textarea>
<p class="row"><button type="submit">Say it</button> <a class="btn ghost" href="/card/{pid}?k={html.escape(k)}">My card</a> <a class="btn ghost" href="/calendar/{pid}.ics?k={html.escape(k)}">Calendar feed</a> <a class="btn ghost" href="/replay/{pid}?k={html.escape(k)}">Watch it work</a></p></form>
{TRUST}""", extra="")


@app.post("/me/{pid}/say")
def say(pid: str, request: Request, text: str = Form(...), k: str = Form("")):
    u = _user(pid)
    if not u or not _may_act(u, request, k):
        return Response("Not yours.", status_code=403)
    text = text.strip()[:4000]
    if not text:
        return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)
    fs = AgentFS(pid)
    fs.log_talk("you", text)
    clear_notes(fs)  # old notes go first, so the consent line pushed below stays unread on the screen
    waking = pid in _WAKING
    if NEED.search(text):  # a request to find someone is card material from now on: the agent may say it on the wire
        if not record_ask(fs, text):
            push_prompt(fs, "note", "I'll look into that quietly: it stays off your card, so I'll ask around without saying why.", log=False)
        else:
            from cahoots.social import _ask_words

            q = push_prompt(fs, "consent", f"I'll tell the agents I write to what you're looking for: \"{_ask_words(text)}\". Put it on your public card too, so agents you haven't met can find you?",
                            ["Put it on my card", "Only tell the agents you write to"], log=False)
            q["ask"] = _ask_words(text)
            _write_ask_marker(fs, q)
    try:
        from cahoots.social import _named_venue_clash

        clash = _named_venue_clash(fs, text)
        MEM.record_text(pid, (f"The person asked about a place their own notes rule out ({clash}); it was refused, this is not a preference." if clash else text))  # AgentCore Memory: the person's own words, recalled at the next reflection
    except Exception:  # noqa: BLE001
        pass
    wk = (fs.root / "talks" / "_wakes.md").read_text().splitlines()[-1:] if (fs.root / "talks" / "_wakes.md").exists() else []
    reflecting = bool(wk) and "reflect on agentcore-runtime: started" in wk[-1]  # the Runtime works on a snapshot; the person lock is free
    if waking and not reflecting:  # the agent is mid-wake: the message is queued now and shown as queued at once, not after a twenty-second wait
        _queue_person(pid, "person", text)
        push_prompt(fs, "note", "Got it. I'm mid-conversation with other agents; I'll answer this the moment that ends, usually within a few minutes.", log=False)
        return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)
    threading.Thread(target=_wake_then_note, args=(pid, "person", text), daemon=True).start()
    return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)


@app.post("/me/{pid}/answer")
def answer(pid: str, request: Request, qid: str = Form(...), answer: str = Form(...), k: str = Form(""), text: str = Form("")):
    u = _user(pid)
    if not u or not _may_act(u, request, k):
        return Response("Not yours.", status_code=403)
    fs = AgentFS(pid)
    if text.strip():  # words typed into a feedback question outrank the button that submitted them
        answer = text.strip()[:300]
    q = answer_prompt(fs, qid, answer)
    if q is None and any(x["id"] == qid and x.get("answer") for x in load_prompts(fs)):
        prior = next(x for x in load_prompts(fs) if x["id"] == qid)
        push_prompt(fs, "note", "That one is already settled" + (" \u2014 I took it back before you tapped, because something about it changed." if prior.get("answer") == "withdrawn" else f", answered {prior.get('answered_at', '')[:16].replace('T', ' ')}.") + " Nothing was booked from this tap.", log=False)
        return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)
    if q and q.get("kind") == "consent":  # the one-tap decision on an ask: public card, or private
        from cahoots.social import approve_ask, keep_ask_private

        if answer.strip().lower().startswith(("put", "keep it on", "yes")):
            approve_ask(fs, q.get("ask", ""))
            push_prompt(fs, "note", "On your card now, under \"Looking for right now\". Edit your card any time to take it off.", log=False)
        else:
            keep_ask_private(fs, q.get("ask", ""))  # off the card, still said to the agents it writes to: the tap means exactly that
            push_prompt(fs, "note", "Off the card. I'll still say what you're looking for to the agents I write to, and only to them.", log=False)
        return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)
    if q:
        try:
            MEM.record_text(pid, f"Asked: {q['text']} Answered: {answer}", session="answers")
        except Exception:  # noqa: BLE001
            pass
        yes = q.get("kind") == "question" and not answer.strip().lower().startswith(("no", "not"))
        threading.Thread(target=_answer_worker, args=(pid, q, answer, yes), daemon=True).start()  # the request returns at once; the booking and the wake happen behind it
    return RedirectResponse(f"/me/{pid}?k={k}", status_code=303)


@app.get("/calendar/{pid}.ics")
def calendar_ics(pid: str, request: Request, k: str = ""):
    u = _user(pid)
    if not u or not _may_act(u, request, k):
        return Response("Not yours.", status_code=403)
    fs = AgentFS(pid)
    booked = _load_json(fs.root / "calendar.json", [])
    invites = [{"invite_id": f"b-{i}", "outing_id": f"b-{i}", "text": (f"{b['where']} with {b['who']}." if str(b.get('who', '')).strip(' ,') else f"{b['where']}."), "guests_preview": [], "reply": "in" if b.get("status", "confirmed") == "confirmed" else "held"} for i, b in enumerate(booked)]
    outs = [{"outing_id": f"b-{i}", "starts_at": b["when"], "duration_min": 120, "venue": {"name": b["where"], "address": "", "neighbourhood": ""}} for i, b in enumerate(booked)]
    return Response(calendar_feed(pid, invites, outs, PUBLIC_BASE, key=u.get("edit_key", "")), media_type="text/calendar; charset=utf-8")


# ---- judges' windows -------------------------------------------------------------------------------
@app.get("/agents", response_class=HTMLResponse)
def agents_page():
    cards = ""
    for pid, url in sorted(NET.state.agents.items(), key=lambda kv: (not kv[0].startswith("u-"), kv[0])):
        c = load_card(AgentFS(pid))
        if not c:
            continue
        cards += (f"<div class='pcard'>{_avatar(c.first_name)}<b>{html.escape(c.first_name)}</b>{' <span class=chip style=\"background:#fff3ea;color:#c8501f\">real person</span>' if pid.startswith('u-') else ' <span class=chip>resident</span>'}"
                  f"<div class='small' style='margin-top:6px'>{html.escape(c.one_liner)}</div><div>{''.join(f'<span class=chip>{html.escape(t)}</span>' for t in c.interests[:3])}</div>"
                  + (f"<div class='small' style='margin-top:6px'><b>Can help with:</b> {html.escape(c.can_help_with)}</div>" if c.can_help_with else "")
                  + (f"<div class='small' style='margin-top:6px'><b>Looking for right now:</b> {html.escape(' | '.join(c.asks))}</div>" if getattr(c, 'asks', None) else "")
                  + f"<div class='small' style='margin-top:8px'><a href='{html.escape(url)}/.well-known/agent-card.json'>A2A agent card</a></div></div>")
    real = sum(1 for p in NET.state.agents if p.startswith("u-"))
    proof = _views("agents")
    return page("The agents", proof + f"<h1>{len(NET.state.agents)} agents on the network</h1><p class='sub'><b>{real} real {'person' if real == 1 else 'people'}</b> and <b>{len(NET.state.agents) - real} residents</b> (seeded so the first real person has someone to meet; their agents run the same code, tools and rails). "
                 f"The real count is the twenty-five friends and family who each pasted their own AI's memory of them, plus the person who built this and the account onboarded on camera in the video. "
                f"Each is a Strands agent served over the A2A protocol at its own URL; what you see on a card is everything its agent may share, and nothing else leaves the folder.</p><div class='grid'>{cards}</div>", wide=True)


def _threads() -> list[dict]:
    """Every DM thread on the network, each rendered from ONE copy: the file of the agent that opened it, which holds
    everything it sent and every reply as it actually crossed the wire (relay output), in the order it happened.
    Other participants' copies are used only when the opener's is gone (a retired account)."""
    names = {pid: _first_name(pid) for pid in list_agents()}
    seen: dict[str, list[str]] = {}
    for pid, n in names.items():
        seen.setdefault(n, []).append(pid)
    for n, pids in seen.items():  # two people who share a first name are told apart on the page by what their card says they are, never by id
        if len(pids) > 1:
            for i, pid in enumerate(sorted(pids)):
                c = load_card(AgentFS(pid)) if (agents_dir() / pid / "card.json").exists() else None
                desc = ""
                if c and c.one_liner:
                    tail = re.sub(r"^\s*[A-Za-z'\-]+,\s*(?:the |an? )?", "", c.one_liner)
                    tail = re.split(r"\s+(?:and|who|which|that|but|with)\b|[,;.]", tail, 1)[0]  # "letterpress printer and choir singer" -> "letterpress printer"
                    desc = " ".join(tail.split()[:3]).strip(" ,.")
                if not desc and c and not pid.startswith("u-") and c.neighbourhood and c.neighbourhood.lower() not in ("unknown", "winnipeg"):
                    desc = c.neighbourhood  # a resident's neighbourhood is invented; a real person's stays off this page
                names[pid] = f"{n} ({desc})" if desc else f"{n} ({'ABCDEFGHJK'[i % 10]})"
    copies: dict[str, list[tuple[bool, Path]]] = {}
    for pid in names:
        for f in (agents_dir() / pid / "threads").glob("*.md"):
            first = next((l for l in f.read_text().splitlines() if l.strip()), "")
            opener = " (me) -> " in first.split(": ", 1)[0]
            copies.setdefault(f.stem, []).append((opener, f))
    out = []
    for tid, fs_ in copies.items():
        fs_.sort(key=lambda t: (not t[0], t[1].stat().st_mtime))
        f = fs_[0][1]
        pid = f.parent.parent.name
        name = names.get(pid, pid)
        lines, who, ids, line_ids = [], set(), {pid}, []
        for line in f.read_text().splitlines():
            m = re.match(r"\[(.+?)\] (.+?) -> (.+?): (.*)$", line)
            if not m:
                continue
            stamp, sender, to, text = m.groups()
            if text.startswith("(late reply) "):
                text = text[len("(late reply) "):]  # a reply that arrived after the sender moved on is still the reply
            bare = re.sub(r"\s*\[guard: [\w-]+\]\s*$", "", text).rstrip()
            if bare.startswith("(") or bare.endswith("assessed the fit from the card only.") or re.search(r"\b(Agent execution failed|Error code:|Traceback|EventLoopException|BadRequestError)\b", bare):
                continue  # a control line, a dead-end stub, or a model failure that an older build let through: never rendered
            mine = sender.endswith("(me)")
            if not mine and sender not in names:
                continue  # not an agent on this network (an old forged envelope): never rendered
            to_ids = [x.strip() for x in to.split(",") if x.strip()]
            if any(x not in names for x in to_ids):
                continue
            sender_name = name if mine else names[sender]
            to_names = ", ".join(names[x] for x in to_ids)
            if re.fullmatch(ID_RE, sender_name or "") or any(re.fullmatch(ID_RE, n) for n in to_names.split(", ")):
                continue  # a retired account's raw id is nobody's name
            if sender_name in to_names.split(", "):
                continue  # an old self-addressed line
            who.update([sender_name] + to_names.split(", "))
            ids.update(([] if mine else [sender]) + to_ids)
            line_ids.append({pid if mine else sender, *to_ids})
            lines.append(f"[{stamp}] {sender_name} → {to_names}: {text}")
        if not lines:
            continue
        first = re.match(r"\[.+?\] (.+?) → ", lines[0]).group(1)
        out.append({"id": tid, "lines": lines, "line_ids": line_ids, "t": f.stat().st_mtime, "who": [first] + sorted(w for w in who if w and w != first), "ids": [pid] + sorted(i for i in ids if i != pid)})
    out.sort(key=lambda t: -t["t"])
    return out


@app.get("/network", response_class=HTMLResponse)
def network(all: int = 0):
    threads = _threads()
    shown = threads if all else threads[:60]
    n_msgs = sum(len(t["lines"]) for t in threads)
    n_guard = sum(1 for t in threads for l in t["lines"] if "[guard: redacted]" in l)
    palette = ["#c8501f", "#2a7f62", "#3b5bdb", "#8e44ad", "#b7791f", "#0e7490", "#be123c", "#4d7c0f"]
    real_names = {n for t in threads for n, i in zip(t["who"], t["ids"]) if i.startswith("u-")} | {_first_name(pid) for pid in list_agents() if pid.startswith("u-")}

    def av(name: str) -> str:
        return f"<span class='av' style='background:{palette[sum(map(ord, name)) % len(palette)]}'>{html.escape(name[:1].upper())}</span>"

    def render(l: str, first: str) -> str:
        m = re.match(r"(\[.+?\]) (.+?) → (.+?): (.*)$", l)
        if not m:
            return f"<div class='m'>{html.escape(l)}</div>"
        stamp, sender, to, text = m.groups()
        tag = ""
        g = re.search(r"\s*\[guard: (\w[\w-]*)\]\s*$", text)
        if g:
            text = text[: g.start()]
            why = {"redacted": "the sender's draft said something not on their card; it was rewritten from the card before it left",
                   "trimmed": "a sentence about someone who is not on this thread was left out; nothing about the sender was changed",
                   "clean": "checked against the sender's card: nothing private", "nothing-private": "nothing private to check",
                   "unchecked": "the guard's model was unavailable, so only card facts were kept"}.get(g.group(1), "")
            short = {"redacted": "rewritten from the card", "trimmed": "a line about a third person left out", "clean": "card only", "nothing-private": "nothing to check", "unchecked": "card facts kept"}.get(g.group(1), "")
            tag = f" <span class='tag {html.escape(g.group(1))}' title='{html.escape(why)}'>guard: {html.escape(g.group(1))}{(' · ' + short) if short else ''}</span>"
        if sender in real_names or any(n.strip() in real_names for n in re.split(r",|&| and ", to or "")):  # a real person's exact hours and budget are card facts for agents, not for anonymous readers (the graph masks them the same way); a resident restating them back is the same disclosure
            text = re.sub(r"\b(?:between\s+|from\s+)?\d{1,2}(?::\d{2})?\s?(?:am|pm)?\s?(?:[-–]|to|until|till|and)\s?\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{2}:\d{2}\s?(?:[-–]|to)\s?\d{2}:\d{2}\b", "[hours]", text, flags=re.I)
            text = re.sub(r"(?:CAD|C?\$)\s?\d{1,4}\b(?:\s?(?:max|cap|budget|a night|per night|or under|or less))?", "[budget]", text, flags=re.I)
        return (f"<div class='bub {'me' if sender == first else ''}'>{av(sender)}<div class='b'><div class='who'><b>{html.escape(sender)}'s agent</b> → {html.escape(to)}'s"
                f" · {html.escape(stamp.strip('[]')[11:])}{tag}</div>{html.escape(text)}</div></div>")

    body = "".join(f"<details class='thread' id='{html.escape(t['id'])}' {'open' if i < 3 else ''}><summary>{av(t['who'][0])}<b>{html.escape(t['who'][0])}</b>&nbsp;↔&nbsp;{html.escape(', '.join(t['who'][1:]))}"
                   f"<span class='small'>· {len(t['lines'])} messages · {html.escape(t['id'])}</span></summary><div class='chat'>"
                   + "".join(render(l, t["who"][0]) for l in (t["lines"] if len(t["lines"]) <= 16 else t["lines"][:10]))
                   + (f"<div class='m'>… {len(t['lines']) - 13} more lines, mostly courtesies after the plan was settled (an older build let these run; a settled thread wakes nobody now)</div>" + "".join(render(l, t["who"][0]) for l in t["lines"][-3:]) if len(t["lines"]) > 16 else "")
                   + "</div></details>" for i, t in enumerate(shown))
    more = (f"<p class='small'>Showing the newest {len(shown)} of {len(threads)} threads. <a href='/network?all=1'>Show all {len(threads)}</a> (a thread's id links straight to it, e.g. /network?all=1#t-299d5a).</p>" if len(threads) > len(shown) else "")
    legend = ("<p class='small'>How to read this: <b>Sam's agent</b> → Olga's is Sam's agent writing to Olga's agent, over A2A. Every recipient on a thread sees the whole "
              f"list, like cc on an email. <span class='tag redacted'>guard: redacted</span> means the sender's message was rewritten from the card before it left, "
              "because it said something the person had not agreed to share. <b>[hours]</b> and <b>[budget]</b> mask a real person's exact free hours and budget on this public page (agents on the network see them; anonymous readers do not). Nothing on this page was typed by a human.</p>")
    pulse = _pulse()
    body = more + body
    return page("What the agents said", _views("network") + f"<h1>What the agents are saying to each other</h1><p class='sub'>{len(threads)} threads and {n_msgs} messages so far (all time), every one checked against the sender's card{' and the Bedrock Guardrail' if not _bedrock_refused() else ''} before it left"
                + (f"; {n_guard} rewritten by the guard" if n_guard else "") + f". Today: {pulse['questions']} questions put to a person, {pulse['booked']} plans booked, {pulse['reflections']} reflection{'s' if pulse['reflections'] != 1 else ''} on AgentCore Runtime. The humans never see this; you do.</p>{legend}{body or '<p>No threads yet.</p>'}"
                "<style>.tag{font:600 12px/1 -apple-system,Segoe UI,Helvetica,Arial;padding:3px 7px;border-radius:9px;background:#e9e4dc;color:#3a3630;vertical-align:middle}.tag.redacted{background:#c8501f;color:#fff}.tag.trimmed{background:#e8a17a;color:#1a1a1a}</style>", wide=True)


@app.post("/admin/wake")
def admin_wake(token: str = Form(""), pid: str = Form(""), reason: str = Form("daily"), payload: str = Form("")):
    if not secrets.compare_digest(str(token or ""), ADMIN_TOKEN):
        return JSONResponse({"error": "bad token"}, status_code=403)
    if not re.fullmatch(ID_RE, pid or "") or not (agents_dir() / pid).exists():
        return JSONResponse({"error": "no such agent"}, status_code=404)
    return {"reply": _wake(pid, reason, payload)}


@app.post("/admin/retire")
def admin_retire(token: str = Form(""), pid: str = Form("")):
    """Retire a test account: its agent leaves the network, its folder and user record are removed. Threads the
    others hold with it stop rendering (unknown ids never render). Only for accounts created while testing."""
    import shutil

    if not secrets.compare_digest(str(token or ""), ADMIN_TOKEN):
        return JSONResponse({"error": "bad token"}, status_code=403)
    if not pid.startswith("u-") or not _user(pid):
        return JSONResponse({"error": "no such user"}, status_code=404)
    wire.unmount_person(NET, pid)
    shutil.rmtree(agents_dir() / pid, ignore_errors=True)
    for other in list_agents():  # residents' plans and notes about the retired account go too
        cal = agents_dir() / other / "calendar.json"
        try:
            if cal.exists():
                booked = json.loads(cal.read_text())
                kept = [b for b in booked if pid not in json.dumps(b)]
                if len(kept) != len(booked):
                    cal.write_text(json.dumps(kept, indent=1))
            (agents_dir() / other / "people" / f"{pid}.md").unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    for f in (USERS / f"{pid}.json", USERS / f"{pid}.pending.json"):
        f.unlink(missing_ok=True)
    return {"retired": pid}


DEMO_PID = os.environ.get("CAHOOTS_DEMO_PID", "u-23bfc4")


def _wake_steps(fs: AgentFS, n: int = 3) -> list[dict]:
    """The last n wakes as lists of tool steps, from the mechanical trace (safe summaries only)."""
    f = fs.root / "talks" / "_tools.md"
    if not f.exists():
        return []
    wakes: list[dict] = []
    for line in f.read_text().splitlines():
        m = re.match(r"\[([^\]]+)\] (\S+) \| (.*)$", line)
        if not m:
            continue
        stamp, reason, rest = m.groups()
        if rest == "## wake":
            wakes.append({"t": stamp, "reason": reason, "steps": []})
            continue
        if not wakes or rest.split(" | ")[0].split(" → ")[0].strip() in ("DmReply", "CardOnly", "LeakVerdict", "Card"):  # framework pseudo-tools, whenever they were written
            continue
        uid = ""
        um = re.search(r" \| #(\w{0,6})$", rest)
        if um:
            uid, rest = um.group(1), rest[: um.start()]
        if " → " not in rest and rest.startswith("calendar | "):  # older trace lines carried when/where: the action only
            rest = "calendar | " + rest.split(" | ", 1)[1].split(" ")[0]
        if " → " in rest:
            name, out = rest.split(" → ", 1)
            if re.match(r"\s*(Error|Traceback|Exception|\d+ validation error)", out):  # a tool that failed: say so, never the raw text
                out = "(failed; the agent carried on)"
            out = _cut(out, 78)  # never mid-word, never an open bracket
            if name.strip() == "dm":  # "thread t-383a3c; waiting" reads as "thread a thread; waiting" once ids are masked: say it in words
                out = "sent; they replied" if out.rstrip().endswith("; reply") else "sent; waiting for a reply" if "; waiting" in out else out
            if name.strip() == "calendar":  # a plan's details never render, whatever an older trace line holds
                low = out.lower()
                out = ("refused" if low.startswith(("not ", "no ", "refus")) else "booked (held)" if low.startswith("booked") else "confirmed" if low.startswith("confirmed")
                       else "a guest added" if low.startswith("added") else "cancelled" if low.startswith("cancel") else "already on the calendar" if low.startswith("already") else "listed")
            target = None
            if uid:
                target = next((st for st in reversed(wakes[-1]["steps"]) if st.get("uid") == uid and "out" not in st), None)
            if target is None:
                target = next((st for st in reversed(wakes[-1]["steps"]) if st["tool"] == name.strip() and "out" not in st), None)
            if target is not None:
                target["out"] = out
            continue
        name, _, arg = rest.partition(" | ")
        if name.strip() == "venues":
            arg = re.sub(r"^\s*(places?|venues?|a place|somewhere)\s+(for|to|near|in)?\s*", "", arg, flags=re.I)
        wakes[-1]["steps"].append({"t": stamp[11:], "tool": name.strip(), "arg": arg, "uid": uid})
    for w in wakes:  # names on the screen, never ids or file paths: the trace shows what the agent did, not the folder's layout
        for st in w["steps"]:
            st["arg"] = _humanise(st.get("arg", ""))
            if "out" in st:
                st["out"] = _humanise(st["out"])
    return [w for w in wakes if w["steps"]][-n:]


def _cut(text: str, n: int) -> str:
    """Shorten on a clause boundary, never mid-word, never leaving a bracket open."""
    t = str(text or "")
    if len(t) <= n:
        return t
    c = t[:n]
    for sep in ("; ", ", ", ". "):
        i = c.rfind(sep)
        if i > n // 2:
            c = c[:i]
            break
    else:
        c = c.rsplit(" ", 1)[0]
    if c.count("(") > c.count(")"):
        c = c[:c.rfind("(")].rstrip()
    return c.rstrip(" ,;:(") + "…"


try:
    _VENUE_NAMES = {v["venue_id"]: v["name"] for v in json.loads((ROOT / "data" / "venues.json").read_text())}
except Exception:  # noqa: BLE001
    _VENUE_NAMES = {}


def _humanise(text: str) -> str:
    """'people/u-de97bc.md' becomes 'its notes on Tobias'; 'threads/t-91b331.md' becomes 'a thread'; a bare id becomes a first name."""
    t = str(text or "")
    t = re.sub(r"people/([pu]-?[0-9a-f]{3,6})\.md", lambda m: f"its notes on {_first_name(m.group(1)) if not re.fullmatch(ID_RE, _first_name(m.group(1)) or '') else 'someone'}", t)
    t = re.sub(r"threads/t-[0-9a-f]{6}\.md", "a thread", t)
    t = re.sub(r"\bme/import\.md\b", "what its person's AI remembered", t)
    t = re.sub(r"\bme\.md\b", "its notes on its person", t)
    t = re.sub(r"\bMEMORY\.md\b", "its memory index", t)
    t = re.sub(r"\bcard\.(md|json)\b", "its card", t)
    t = re.sub(r"\bcalendar\.json\b", "its calendar", t)
    t = re.sub(r"\bprompts\.json\b", "its questions", t)
    t = re.sub(r"\bouting[s]?/[\w\-]+\.md\b", "an outing's notes", t)
    t = re.sub(r"\btalks/[\w\-]+\.md\b", "the day's talk log", t)
    t = re.sub(r"\bnotes/[\w\-]+\.md\b", "a note", t)
    t = re.sub(r"\bgroups?/[\w\-]+\.(md|json)\b", "a group's notes", t)
    t = re.sub(r"\b\d+ chars\b", "read", t)
    for vid, name in _VENUE_NAMES.items():
        t = re.sub(rf"\b{re.escape(vid)}\b:?", name, t)
    t = re.sub(r"\bwrote (its notes on [^(]+?) \(\d+ chars\)", r"wrote \1", t)
    t = re.sub(r"\bt-[0-9a-f]{6}\b", "a thread", t)
    t = re.sub(r"\bq-[0-9a-f]{6}\b", "a question", t)
    t = re.sub(r"\b([pu]-?[0-9a-f]{3,6})\b", lambda m: (_first_name(m.group(1)) if not re.fullmatch(ID_RE, _first_name(m.group(1)) or "") else "someone"), t)
    return t


_VERB = {"read": "reads", "write": "writes", "append": "adds to", "ls": "lists", "grep": "searches its notes for", "remember": "remembers", "find_agents": "searches the directory for",
         "venues": "looks up places for", "dm": "writes to", "pass_behaviour": "passes on, for next time, to", "ask_human": "asks its person:", "notify_human": "tells its person:", "calendar": "calendar:", "group": "group:"}


def _substantial(wakes: list[dict], n: int = 3) -> list[dict]:
    """For the public page: the wakes that show the network at work (a directory search, a message to another agent, a question), not three one-step replies."""
    for w in wakes:  # a refused attempt ("not sent", "not asked") is the harness saying no, not a step the agent took
        w["steps"] = [st for st in w["steps"] if not str(st.get("out", "")).startswith(("not sent", "not asked", "not booked", "not written", "not appended", "not cancelled", "not confirmed"))]
    def rich(w):  # a search, several messages, a question: the wake that shows the network at work
        tools = [st["tool"] for st in w["steps"]]
        return (3 if "find_agents" in tools else 0) + min(tools.count("dm"), 4) * 2 + (3 if "ask_human" in tools else 0) + min(len(tools), 12) / 4
    good = sorted((w for w in wakes if rich(w) >= 5), key=rich, reverse=True)[:n]
    return good or wakes[-n:]


def _replay_html(first: str, wakes: list[dict], public: bool) -> str:
    if not wakes:
        return "<p class='small'>No wake recorded yet.</p>"
    blocks = []
    for w in reversed(wakes):
        def _kind(path: str) -> str:  # what kind of file, never which one
            low = path.lower()
            if low.startswith("people/"): return "its notes on a person"
            if low.startswith("threads/"): return "a thread"
            if low.startswith("talks/"): return "a conversation log"
            if low.startswith("groups/"): return "a group's notes"
            if low.startswith(("me.md", "me/")): return "its notes on its person"
            if low.startswith("memory.md"): return "its memory index"
            if low.startswith("card"): return "its card"
            if low.startswith("calendar"): return "its calendar"
            if low.startswith("asks"): return "its person's asks"
            return "a file"
        def _arg(st):  # the public replay names the tool and, for messages, the recipients; a search term or a file name could carry a private word
            if public and st["tool"] in ("read", "write", "append"):
                return _kind(st["arg"])
            if public and st["tool"] in ("grep", "find_agents", "venues", "remember", "ask_human", "notify_human"):
                return {"grep": "a word", "find_agents": "", "venues": "", "remember": "a note", "ask_human": "a question", "notify_human": "a note"}.get(st["tool"], "")  # "" so the verb stands alone: "looks up places for places" was the old rendering
            return st["arg"]
        rows: list[dict] = []  # consecutive identical public lines collapse into one with a count ("reads its notes on a person ×4")
        for st in w["steps"]:
            verb, arg = _VERB.get(st["tool"], st["tool"]), _arg(st)
            if not arg:
                verb = re.sub(r"\s+(for|to)$", "", verb).rstrip(":")  # nothing follows the verb, so it does not dangle
            label = (verb, arg)
            shown_out = st.get("out") if st.get("out") and not (public and st["tool"] in ("read", "grep", "write", "append", "remember", "ls")) else ""
            if rows and public and rows[-1]["label"] == label and not shown_out and not rows[-1]["out"]:
                rows[-1]["n"] += 1
                continue
            rows.append({"t": st["t"], "label": label, "out": shown_out, "n": 1})
        steps = "".join(f"<li class='step' data-i='{i}'><span class='t'>{html.escape(r['t'])}</span> <b>{html.escape(r['label'][0])}</b> {html.escape(r['label'][1])}"
                        + (f" <span class='out'>×{r['n']}</span>" if r["n"] > 1 else "") + (f" <span class='out'>→ {html.escape(r['out'][:90])}</span>" if r["out"] else "") + "</li>" for i, r in enumerate(rows))
        blocks.append(f"<details class='thread' open><summary><b>{html.escape(first)}'s agent</b>&nbsp;· wake <code>{html.escape(w['reason'])}</code> · {html.escape(w['t'])} · {len(w['steps'])} steps</summary><ol class='replay'>{steps}</ol></details>")
    return ("<p class='small'>Every line is a real tool call the agent made, in order, from its own log. On this public page you see the tool, the recipients of a message and the kind of action; file names, search terms and plan details show only on the person's own replay. "
            + ("This is the demo account; the person's own screen has the same replay of their agent." if public else "This is your agent, as it worked.") + "</p>"
            + "".join(blocks)
            + "<style>.replay{padding-left:22px}.replay .step{margin:6px 0;transition:background .6s;border-radius:6px}.replay .step.on{background:#fff3ea}.replay .t{color:var(--muted);font-size:12px;margin-right:6px}.replay .out{color:var(--muted)}</style>"
            "<script>const ol=document.querySelector('ol.replay'); if(ol){[...ol.children].forEach((li,i)=>setTimeout(()=>{li.classList.add('on'); setTimeout(()=>li.classList.remove('on'),700);}, i*450+200));}</script>")


@app.get("/replay", response_class=HTMLResponse)
def replay_demo():
    """A judge can watch one full wake of the demo account's agent: every tool call in order, safely summarised."""
    fs = AgentFS(DEMO_PID)
    return page("Watch a wake", _views("replay") + f"<h1>Watch a wake</h1><p class='sub'>What one Strands agent actually does when it wakes: reads its notes, searches the directory, writes to other agents over A2A, and asks its person one question.</p>"
                + _replay_html(_first_name(DEMO_PID), _substantial(_wake_steps(fs, 40)) if (fs.root / "card.json").exists() else [], public=True), wide=True)


@app.get("/replay/{pid}", response_class=HTMLResponse)
def replay_person(pid: str, request: Request, k: str = ""):
    u = _user(pid)
    k = k or request.cookies.get(f"ck_{pid}", "")
    if not u or not _may_act(u, request, k):
        r = page("Cahoots", "<h1>Not your agent.</h1><p class='sub'>Open the link you got when you onboarded.</p>"); r.status_code = 403; return r
    return page("Your agent, step by step", f"<h1>Your agent, step by step</h1>" + _replay_html(u["first_name"], _wake_steps(AgentFS(pid), 5), public=False)
                + f"<p class='row' style='margin-top:18px'><a class='btn ghost' href='/me/{pid}?k={html.escape(k)}'>Back to your screen</a></p>", wide=True)


def graph_embed(focus: str = "") -> HTMLResponse:
    full = graph_page(focus, 1, 0, 0, 1).body.decode()
    css = ("<style>nav.nav,footer.foot,h1,p.sub,p.views,main>p.small{display:none!important}main{max-width:none;padding:0!important}body{background:#fff}"
           "canvas{border:0!important;border-radius:0!important}#legend{font-size:12px!important;top:8px!important;left:8px!important}#replay{top:8px!important;right:8px!important}</style>")
    return HTMLResponse(full.replace("</head>", css + "</head>", 1))


@app.get("/graph.json")
def net_graph(focus: str = ""):
    if not re.fullmatch(ID_RE, focus or ""):
        focus = ""
    """The network as a graph, card-level only: who is on it, who has talked to whom (from the threads), in the order it
    happened. Nodes carry a first name, real/resident, neighbourhood and what they can help with; never anything private."""
    names = {pid: _first_name(pid) for pid in list_agents() if (agents_dir() / pid / "card.json").exists()}  # an approved card, or no dot
    nodes = []
    for pid in sorted(names):
        c = load_card(AgentFS(pid)) if (agents_dir() / pid / "card.json").exists() else None
        nodes.append({"id": pid, "name": names[pid], "real": pid.startswith("u-"), "hood": ("" if pid.startswith("u-") else (c.neighbourhood if c else "")),  # a real person's neighbourhood stays off anonymous surfaces
                      "offers": (c.can_help_with if c else ""), "asks": (list(getattr(c, "asks", []) or []) if c else []), "interests": (c.interests[:3] if c else [])})
    edges: dict[tuple, dict] = {}
    for t in _threads():
        ids = [i for i in t.get("ids", []) if i in names]  # by id: two people who share a first name never cross-link
        first = t["lines"][0][1:17] if t["lines"] else ""
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                k = tuple(sorted((a, b)))
                e = edges.setdefault(k, {"a": k[0], "b": k[1], "n": 0, "t": first})
                e["n"] += sum(1 for li in t.get("line_ids", []) if a in li and b in li) or 1  # messages that actually passed between this pair
                e["t"] = min(e["t"], first) if e["t"] else first
    booked = set()
    for pid in names:
        for b in _load_json(agents_dir() / pid / "calendar.json", []):
            booked.add(pid)
    return {"nodes": nodes, "edges": sorted(edges.values(), key=lambda e: e["t"]), "booked": sorted(booked), "focus": focus}


@app.get("/graph", response_class=HTMLResponse)
def graph_page(focus: str = "", autoplay: int = -1, big: int = 0, embed: int = 0, fast: int = 0):
    """An animated picture of the network: every agent a dot, every thread a line, drawn in the order it happened.
    The whole-network page opens fully drawn (Replay animates it); a focus page and the homepage embed animate from empty."""
    if embed:  # the homepage shows the live picture above the fold: canvas and caption only, links back to the full page
        return graph_embed(focus)
    if not re.fullmatch(ID_RE, focus or ""):  # only an agent id ever reaches the page's script
        focus = ""
    if not focus and not embed and (agents_dir() / DEMO_PID / "card.json").exists():  # opening cold: the demo account's threads highlighted, so the picture has a subject
        focus = DEMO_PID
    if autoplay < 0:
        autoplay = 1 if (focus or fast) else 0
    return page("The network", (_views("graph") if not embed else "") + f"""
<h1>The network, drawn as it happened</h1>
<p class="sub">Every dot is one person with one agent, placed around the ring by neighbourhood so that neighbours sit together. Orange is a real person; grey is one of the sixty residents we invented so the first real person had someone to meet. Every line is a thread between two agents, drawn in the order it happened; thicker means more messages. Nothing here is private: names, neighbourhoods and what people can help with are card facts.</p>
<div style="position:relative"><canvas id="g" width="1200" height="720" style="width:100%;max-width:1200px;border:1px solid var(--line);border-radius:16px;background:#fff"></canvas>
<div id="legend" style="position:absolute;left:16px;top:14px;background:rgba(255,255,255,.92);border:1px solid #e3ddd3;padding:8px 12px;border-radius:10px;font-size:14px;line-height:1.6"><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#c8501f;vertical-align:middle"></span> you (or the person in focus) &nbsp; <span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#e8a17a;vertical-align:middle"></span> a real person &nbsp; <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#7a746b;vertical-align:middle"></span> a resident &nbsp; <span style="display:inline-block;width:22px;height:3px;background:#c8501f;vertical-align:middle"></span> their conversations &nbsp; <span style="display:inline-block;width:22px;height:2px;background:#bbb;vertical-align:middle"></span> everyone else's<div style="margin-top:5px;padding-top:5px;border-top:1px solid #eee5da;color:#6b665f">Position is <b>neighbourhood</b>: neighbours sit next to each other round the ring. Colour is who they are.</div></div>
<button id="replay" class="ghost" style="position:absolute;right:16px;top:14px;padding:8px 14px;font-size:14px">Replay</button>
<div id="cap" style="position:absolute;left:16px;bottom:16px;background:rgba(26,26,26,.9);color:#fff;padding:10px 14px;border-radius:12px;font-size:16px;max-width:70%"></div></div>
<p class="small">Click a dot to open that person's card. Threads between two agents are drawn in the order they happened; the person in focus is drawn first.</p>
<script>
const FOCUS = {json.dumps(focus).replace("<", "\\u003c")}; const AUTOPLAY = {int(bool(autoplay))}; const BIG = {int(bool(big))}; const FAST = {int(bool(fast))};
fetch("/graph.json").then(r=>r.json()).then(g=>{{
  const cv=document.getElementById('g'), ctx=cv.getContext('2d'), W=cv.width, H=cv.height, cap=document.getElementById('cap');
  const nodes=g.nodes, idx={{}}; nodes.forEach((n,i)=>idx[n.id]=i);
  // layout: one ring, everybody on it, sorted by neighbourhood so neighbours sit together; the focus at the centre. Colour alone says who is real: a second ring would encode the same fact twice and imply a hierarchy that does not exist.
  const ring=nodes.filter(n=>n.id!==FOCUS).sort((a,b)=>(a.hood||'').localeCompare(b.hood||'')||a.name.localeCompare(b.name));
  const CY=H/2-6;  // room for the legend above and the caption below
  ring.forEach((n,i)=>{{ const ang=-Math.PI/2+(i/ring.length)*Math.PI*2; n.x=W/2+Math.cos(ang)*(W*0.40); n.y=CY+Math.sin(ang)*(H*0.36); }});
  nodes.forEach(n=>{{ if(n.id===FOCUS){{ n.x=W/2; n.y=CY; }} }});
  let edges=g.edges.filter(e=>idx[e.a]!==undefined&&idx[e.b]!==undefined);
  if(FOCUS){{ edges=[...edges.filter(e=>e.a===FOCUS||e.b===FOCUS), ...edges.filter(e=>e.a!==FOCUS&&e.b!==FOCUS)]; }}
  let shown=0, t0=performance.now(); const per=AUTOPLAY?(FOCUS?650:(FAST?70:180)):0; const S=BIG?1.7:1;
  document.getElementById('replay').onclick=()=>{{shown=0;t0=performance.now();requestAnimationFrame(draw);}};
  cv.onclick=(ev)=>{{const r=cv.getBoundingClientRect(); const x=(ev.clientX-r.left)*W/r.width, y=(ev.clientY-r.top)*H/r.height; const n=nodes.find(n=>Math.hypot(n.x-x,n.y-y)<14*S); if(n) location.href='/agents#'+n.id;}};
  function draw(){{
    ctx.clearRect(0,0,W,H);
    const lit=new Set();
    for(let k=0;k<shown;k++){{ const e=edges[k]; const a=nodes[idx[e.a]], b=nodes[idx[e.b]]; const hot=(e.a===FOCUS||e.b===FOCUS);
      ctx.strokeStyle=hot?'rgba(200,80,31,.85)':(FOCUS?'rgba(120,110,100,.25)':'rgba(120,110,100,.32)'); ctx.lineWidth=S*Math.min(1+e.n/4, hot?6:(FOCUS?2.5:3)); ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); lit.add(e.a); lit.add(e.b); }}
    const hotIds=new Set(); for(let k=Math.max(0,shown-5);k<shown;k++){{ hotIds.add(edges[k].a); hotIds.add(edges[k].b); }}
    const done=shown>=edges.length;
    nodes.forEach(n=>{{ const isF=n.id===FOCUS; const r=S*(isF?16:(n.real?10:7)); ctx.beginPath(); ctx.arc(n.x,n.y,r,0,Math.PI*2);
      ctx.fillStyle=isF?'#c8501f':(n.real?'#e8a17a':(lit.has(n.id)?'#7a746b':'#d8d2c8')); ctx.fill();
      const label=isF||n.real||(BIG?(hotIds.has(n.id)||(done&&lit.has(n.id))):lit.has(n.id));  // while the big picture animates, only the agents talking right now are named
      if(label){{ ctx.fillStyle='#1a1a1a'; ctx.font=(isF?'bold '+Math.round(18*S)+'px':Math.round(13*(BIG?1.35:1))+'px')+' -apple-system,Segoe UI,Helvetica,Arial';
        const left=n.x<W/2&&!isF&&!n.real; ctx.textAlign=left?'right':'left'; ctx.fillText(n.name, left?n.x-r-5:n.x+r+5, n.y+5); ctx.textAlign='left'; }} }});
    if(shown<edges.length){{ if(performance.now()-t0>per){{ shown++; t0=performance.now(); const e=edges[shown-1]; const a=nodes[idx[e.a]], b=nodes[idx[e.b]];
        cap.textContent=`${{a.name}}'s agent ↔ ${{b.name}}'s agent · ${{e.n}} message${{e.n===1?'':'s'}}` + (b.offers&&(e.a===FOCUS)?` · can help with ${{b.offers.split(';')[0]}}`:''); }} requestAnimationFrame(draw); }}
    else {{ cap.textContent=`${{nodes.length}} agents · ${{edges.length}} conversations · ${{g.booked.length}} people with a plan on the calendar`; }}
  }}
  draw();
}});
</script>""", wide=True)


@app.get("/version")
def version():
    v = (ROOT_DIR / "VERSION").read_text().strip() if (ROOT_DIR / "VERSION").exists() else "dev"
    return {"commit": v}


@app.post("/admin/folder")
def admin_folder(token: str = Form(""), pid: str = Form("")):  # POST: the token never sits in a URL, a log or a referrer
    if not secrets.compare_digest(str(token or ""), ADMIN_TOKEN):
        return JSONResponse({"error": "bad token"}, status_code=403)
    if not re.fullmatch(ID_RE, pid or "") or not (agents_dir() / pid).exists():
        return JSONResponse({"error": "no such agent"}, status_code=404)
    return AgentFS(pid).snapshot()
