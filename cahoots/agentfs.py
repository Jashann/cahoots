"""Each agent keeps its person in plain markdown, in its own folder. The harness logs mechanically;
the agent writes meaning. Layout under work/agents/<person_id>/:

  MEMORY.md         always in the agent's context: one line per thing that matters, with a pointer
  me.md             who my person is, what lights them up, what they hate, what they're after, guesses to test
  card.md           the approved public card: the ONLY file whose contents may leave this folder
  me/import.md      the pasted AI memory, kept privately so the agent can go back for detail
  people/<id>.md    everyone my person has met: what I noticed, how it went, trust
  groups/<id>.md    my notes on groups my person is in
  threads/<id>.md   every DM thread, appended by the harness as it happens
  talks/<date>.md   every exchange with my person, appended by the harness as it happens
  outings/<id>.md   each plan: who, where, what happened, feedback
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def work_dir() -> Path:
    return Path(os.environ.get("CAHOOTS_WORK_DIR") or (ROOT / "work"))


def agents_dir() -> Path:
    return work_dir() / "agents"


SUBDIRS = ("me", "people", "groups", "threads", "talks", "outings")


def _now():
    from cahoots.social import local_now

    return local_now()


NEED = re.compile(r"\b(find me (a|an|someone|somebody|people)|find (someone|somebody|a person|people) (who|that|to|for)|i(?:'m| am) looking for (a|an|someone|somebody|people)|"
                  r"i need (someone|somebody|a person|people|to meet|to find)|i need (a|an) (?:[a-z-]+ ){0,2}(who|that|for my|for the|in town|to help)|who (can|could) (help|do|make|build|teach|manufacture|introduce)|"
                  r"introduce me to|connect me (with|to)|i want to meet (a|an|someone|somebody|people)|can you find (me )?(a|an|someone|somebody|people))\b", re.I)


PLAN_TALK = re.compile(r"\b(book|booked|move (it|the plan)|change (the )?(venue|place|time|day)|different (venue|place|time|day)|reschedule|instead of|somewhere else|another (place|time|day)|"
                       r"cancel|call it off|confirm|let'?s do|make it|(mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}(:\d{2})? ?(am|pm)|\d{4}-\d{2}-\d{2})\b", re.I)


ID_RE = r"(p\d{3}|u-[a-z0-9]{2,12})"  # a person id is a folder name; tests use short ids, the app mints u-<6 hex>
ID_STRIP = r"\s*\((?:p\d{3}|u-[a-z0-9]{2,12})\)"


class AgentFS:
    """One person's folder, with the safety rule that every path stays inside it."""

    def __init__(self, person_id: str):
        if not re.fullmatch(ID_RE, person_id or ""):
            raise PermissionError("not a person id")  # an id is a folder name: never a path
        self.person_id = person_id
        self.root = (agents_dir() / person_id).resolve()
        for d in SUBDIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if not (self.root / "MEMORY.md").exists():
            (self.root / "MEMORY.md").write_text("")

    # ---- safety ------------------------------------------------------------------------
    def path(self, rel: str) -> Path:
        rel = (rel or "").strip().lstrip("/")
        p = (self.root / rel).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise PermissionError("that path is outside your folder")
        return p

    # ---- plain file ops (the tools wrap these) -------------------------------------------
    def read(self, rel: str, max_chars: int = 12000) -> str:
        p = self.path(rel)
        if not p.exists():
            return f"(no such file: {rel})"
        if p.is_dir():
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
        text = p.read_text()
        return text if len(text) <= max_chars else text[-max_chars:]

    def write(self, rel: str, text: str) -> str:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return f"wrote {rel} ({len(text)} chars)"

    def append(self, rel: str, text: str) -> str:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(text.rstrip("\n") + "\n")
        return f"appended to {rel}"

    def ls(self, rel: str = "") -> str:
        p = self.path(rel)
        if not p.exists():
            return f"(no such folder: {rel})"
        out = []
        for x in sorted(p.rglob("*") if rel == "" else p.iterdir()):
            if x.is_file():
                out.append(str(x.relative_to(self.root)) + f"  ({x.stat().st_size} B)")
        return "\n".join(out) or "(empty)"

    def grep(self, pattern: str, rel: str = "", max_hits: int = 40) -> str:
        p = self.path(rel)
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"bad pattern: {e}"
        hits = []
        files = [p] if p.is_file() else sorted(p.rglob("*.md"))
        for f in files:
            try:
                for n, line in enumerate(f.read_text().splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.root)}:{n}: {line.strip()[:200]}")
                        if len(hits) >= max_hits:
                            return "\n".join(hits) + f"\n(stopped at {max_hits} hits)"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    # ---- the always-in-context index ----------------------------------------------------
    def memory(self) -> str:
        return (self.root / "MEMORY.md").read_text()

    def remember(self, line: str) -> str:
        line = " ".join((line or "").split())
        if not line:
            return "nothing to remember"
        self.append("MEMORY.md", "- " + line if not line.startswith("- ") else line)
        return "remembered"

    def card_text(self) -> str:
        p = self.root / "card.md"
        return p.read_text() if p.exists() else ""

    def card_public(self) -> str:
        """The card as a stranger may see it: who they are and what they like, free DAYS only, no hours, no budget,
        no notes. Agents on the network (signed) get the whole card; exact weekly hours plus a neighbourhood are
        not for anonymous callers."""
        out = []
        for line in self.card_text().splitlines():
            l = line.strip()
            if l.startswith("- Free:"):
                days = sorted({w for w in re.findall(r"\b(mon|tue|wed|thu|fri|sat|sun)\b", l)}, key=["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index)
                out.append("- Free: " + ", ".join(days) if days else "- Free: ask their agent")
            elif l.startswith(("- Around", "- Also:", "- Group size", "- Looking for right now")):  # asks are for agents on the network, not for anonymous callers
                continue
            else:
                out.append(line)
        return "\n".join(out)

    # ---- mechanical logs (never the agent's choice) -------------------------------------
    def log_talk(self, who: str, text: str) -> None:
        day = _now().strftime("%Y-%m-%d")
        one_line = " / ".join(l.strip() for l in (text or "").splitlines() if l.strip())
        self.append(f"talks/{day}.md", f"[{_now().strftime('%H:%M')}] {who}: {one_line}")

    def log_thread(self, thread_id: str, sender: str, to: list[str], text: str) -> None:
        one_line = " / ".join(l.strip() for l in (text or "").splitlines() if l.strip())
        self.append(f"threads/{thread_id}.md", f"[{_now().strftime('%Y-%m-%d %H:%M')}] {sender} -> {', '.join(to)}: {one_line}")

    def own_words(self, limit_lines: int = 200) -> list[str]:
        """The person's own words to their agent (the last two weeks of talks): private by definition."""
        out: list[str] = []
        for f in sorted(x for x in (self.root / "talks").glob("*.md") if not x.name.startswith("_"))[-14:]:
            out += [l.split(": ", 1)[1] for l in f.read_text().splitlines() if ": " in l and "] you:" in l]
        # questions to the agent ("who is Dorothy?"), small talk under five words and requests to find someone are not secrets; statements are
        return [w for w in out if w and not w.strip().endswith("?") and len(w.split()) >= 5 and not NEED.search(w) and not PLAN_TALK.search(w)][-limit_lines:]

    def note_lines(self, limit_lines: int = 400) -> list[str]:
        """What the agent wrote down about its person (me.md, MEMORY.md): private where it says more than the
        card does. Notes about other people (people/) are what their agents said over the wire or what the
        person said (already in own_words), so they are not guarded twice."""
        out: list[str] = []
        for rel in ["me.md", "MEMORY.md"]:
            p = self.root / rel
            if p.exists():
                out += [l.strip("- #").strip() for l in p.read_text().splitlines() if l.strip()]
        return [w for w in out if w][-limit_lines:]

    def private_words(self, limit_lines: int = 400) -> list[str]:
        """Everything the guard treats as private material (own words first, then notes)."""
        return (self.own_words() + self.note_lines())[-limit_lines:]

    def snapshot(self) -> dict:
        """Everything in the folder (markdown and the small json state), for shipping to the AgentCore Runtime and back."""
        return {str(p.relative_to(self.root)): p.read_text() for p in sorted(self.root.rglob("*")) if p.is_file() and p.suffix in (".md", ".json")}

    def restore(self, files: dict) -> None:
        for rel, text in files.items():
            self.write(rel, text)


def list_agents() -> list[str]:
    return sorted(p.name for p in agents_dir().iterdir() if p.is_dir()) if agents_dir().exists() else []
