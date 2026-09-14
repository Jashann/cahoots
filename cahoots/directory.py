"""The phone book. Every agent lists its person's card here; an agent searches it before it talks to
anyone, so in a city of millions a feeler goes to a shortlist, never to everyone.

The demo index is in-process keyword scoring over cards (neighbourhood, free days, interests, likes,
modes, one-liner); the interface is the thing, the index behind it is replaceable."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cahoots.agentfs import ROOT, agents_dir

_STOP = set("the and with for into like likes love loves who that this from some more very people person free night nights out".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z\-']{2,}", (text or "").lower()) if w not in _STOP}


def card_entry(card: dict) -> dict:
    """What the directory holds about a person: the card, nothing else."""
    return {
        "person_id": card["person_id"], "first_name": card["first_name"], "one_liner": card.get("one_liner", ""), "can_help_with": card.get("can_help_with", ""),
        "neighbourhood": card.get("neighbourhood", ""), "interests": card.get("interests", []), "likes": card.get("likes", []),
        "avoids": card.get("avoids", []), "modes": card.get("modes", []), "budget_cad": card.get("budget_cad", 0),
        "free": sorted({a["day"] for a in card.get("availability", [])}), "availability": card.get("availability", []),
    }


def windows_overlap(a: list, b: list) -> str:
    """Where two availability lists meet, by weekday ("sat 18:00-20:00, sun 12:00-15:00"); empty when they never do. Entries may be dicts or objects with day/start/end."""
    def f(w):
        return (getattr(w, "day", None) or w.get("day"), getattr(w, "start", None) or w.get("start"), getattr(w, "end", None) or w.get("end"))
    out = []
    try:
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            for da, sa, ea in [f(w) for w in a if f(w)[0] == day]:
                for db, sb, eb in [f(w) for w in b if f(w)[0] == day]:
                    start, end = max(sa, sb), min(ea, eb)
                    if start < end:
                        out.append(f"{day} {start}-{end}")
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(out)


class Directory:
    def __init__(self, entries: list[dict]):
        self.entries = {e["person_id"]: e for e in entries}

    @classmethod
    def load(cls) -> "Directory":
        """Personas from data/, real people from their agent folders (card.json next to card.md)."""
        entries = [card_entry(c) for c in json.loads((ROOT / "data" / "personas.json").read_text())]
        if agents_dir().exists():
            for d in agents_dir().iterdir():
                cj = d / "card.json"
                if cj.exists():
                    try:
                        entries.append(card_entry(json.loads(cj.read_text())))
                    except (ValueError, KeyError):
                        pass
        return cls(entries)

    def add(self, card: dict) -> None:
        self.entries[card["person_id"]] = card_entry(card)

    def search(self, query: str, exclude: str | None = None, k: int = 12, overlap_with: list | None = None) -> list[dict]:
        """Keyword search with the card's structure: interests and one-liner count most, then vibes, modes,
        neighbourhood and free days. With the searcher's own windows (overlap_with), people who are free at the same
        time rank first and carry a "free together" line; once three or more overlap, the rest are left out, because a
        plan with someone who is never free when you are costs a wake and books nothing. Returns ranked entries, never more than k."""
        q = _tokens(query)
        days = {d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") if re.search(rf"\b{d}(?:day|s|nes|rs|ur)?\w*\b", query.lower())}  # "saturday" or "sat", never "sat" inside "satisfying"
        scored = []
        for e in self.entries.values():
            if e["person_id"] == exclude:
                continue
            s = 0.0
            s += 3 * len(q & _tokens(" ".join(e["interests"]) + " " + e["one_liner"]))
            s += 4 * len(q & _tokens(e.get("can_help_with", "")))  # what someone can offer weighs most when the ask is a need
            s += 2 * len(q & set(e["likes"]))
            s += 1 * len(q & set(e["modes"]))
            s += 2 * len(q & _tokens(e["neighbourhood"]))
            s -= 3 * len(q & set(e["avoids"]))
            if days and days & set(e["free"]):
                s += 2
            if s > 0:
                e = dict(e)
                if overlap_with:
                    together = windows_overlap(overlap_with, e.get("availability", []))
                    e["_together"] = together
                    s += 3 if together else -2
                scored.append((s, e))
        scored.sort(key=lambda t: -t[0])
        out = [e for _, e in scored]
        if overlap_with and sum(1 for e in out if e.get("_together")) >= 3:
            out = [e for e in out if e.get("_together")]
        return out[:k]

    def describe(self, e: dict) -> str:
        kind = "resident, answers now" if e["person_id"].startswith("p") else "real person: their agent must ask them, which can take a day"
        together = (f" FREE TOGETHER {e['_together']} (propose inside this)" if e.get("_together") else " (never free when your person is: only worth a message with a specific reason)") if "_together" in e else ""
        return (f"{e['person_id']}: {e['one_liner']} ({e['neighbourhood']}); into {', '.join(e['interests'][:4])}; "
                + (f"can help with {e['can_help_with']}; " if e.get("can_help_with") else "")
                + f"likes {', '.join(e['likes'])}; open to {', '.join(e['modes'])}; free {', '.join(e['free'])}{together} [{kind}]")
