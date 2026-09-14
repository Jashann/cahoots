"""How the one message reaches the human: an email through Amazon SES (when a sender is configured)
and a calendar feed anyone can subscribe to. Both carry only what the invite carries: when, where, who
by first name, and the three answers as links. Nothing from the Card beyond first names, nothing private.

Email: set CAHOOTS_SES_FROM to a verified SES identity; people give an address on their card form (it
is stored with their user record, never on the Card, so no agent ever sees it).
Calendar: GET /calendar/{person_id}.ics lists that person's invites as TENTATIVE events until they tap
In, then CONFIRMED; Google/Apple Calendar can subscribe to the URL.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta
import re as _re

REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM = os.environ.get("CAHOOTS_SES_FROM", "")


def email_enabled() -> bool:
    return bool(SES_FROM)


def _links(base: str, pid: str, invite_id: str, key: str = "") -> dict[str, str]:
    q = f"?k={key}" if key else ""
    return {r: f"{base}/me/{pid}{q}" for r in ("in", "not_this_one", "not_this_week")}  # every reply happens on the one screen


def invite_email(base: str, pid: str, invite: dict, first_name: str, key: str = "") -> tuple[str, str, str]:
    """(subject, text body, html body) for one invite. The links carry the person's key: the email is theirs."""
    links = _links(base, pid, invite["invite_id"], key)
    guests = ", ".join(invite.get("guests_preview", []))
    subject = f"Cahoots: {invite['text'][:70].rstrip('.?!')}…" if len(invite["text"]) > 70 else f"Cahoots: {invite['text']}"
    text = (f"Hi {first_name}.\n\n{invite['text']}\n\nWith: {guests}\n\nIn: {links['in']}\nNot this one: {links['not_this_one']}\n"
            f"Not this week: {links['not_this_week']}\n\nYour agent made this plan from your card only. Reply with one tap; nothing happens until you do.\n")
    body = (f"<p>Hi {html.escape(first_name)}.</p><p style='font-size:18px'>{html.escape(invite['text'])}</p><p style='color:#666'>With: {html.escape(guests)}</p>"
            f"<p><a href='{links['in']}' style='background:#c8501f;color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none'>In</a> &nbsp; "
            f"<a href='{links['not_this_one']}'>Not this one</a> &nbsp; <a href='{links['not_this_week']}'>Not this week</a></p>"
            f"<p style='color:#666;font-size:13px'>Your agent made this plan from your card only. Nothing happens until you tap In.</p>")
    return subject, text, body


def send_invite_email(to: str, base: str, pid: str, invite: dict, first_name: str, key: str = "") -> bool:
    """Send one invite through SES. Returns True when SES accepted it; never raises (a failed email must
    not fail the week; the inbox page still has the invite)."""
    if not email_enabled() or not to:
        return False
    try:
        import boto3

        subject, text, body = invite_email(base, pid, invite, first_name, key)
        boto3.client("sesv2", region_name=REGION).send_email(
            FromEmailAddress=SES_FROM, Destination={"ToAddresses": [to]},
            Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": text}, "Html": {"Data": body}}}},
        )
        return True
    except Exception as e:  # noqa: BLE001
        print("invite email failed:", e)
        return False


def _ics_escape(s: str) -> str:
    return re.sub(r"([,;\\])", r"\\\1", s).replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: content lines are at most 75 octets; continuation lines start with a space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur); cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def calendar_feed(pid: str, invites: list[dict], outings: list[dict], base: str, key: str = "") -> str:
    """An iCalendar feed of this person's invites: TENTATIVE until they tap In, CONFIRMED after."""
    by_id = {o["outing_id"]: o for o in outings}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Cahoots//invites//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Cahoots", "X-WR-TIMEZONE:America/Winnipeg", "X-WR-CALDESC:One plan a week, made by your agent. Tentative until you tap In."]
    for i in invites:
        i = dict(i, text=_re.sub(r"\s*\((?:p\d{3}|u-[0-9a-f]{6})\)", "", i.get("text", "")))  # first names, never ids
        o = by_id.get(i["outing_id"])
        if not o:
            continue
        try:
            start = datetime.fromisoformat(o["starts_at"])
        except (KeyError, ValueError):
            continue
        end = start + timedelta(minutes=int(o.get("duration_min", 120)))
        status = "CONFIRMED" if i.get("reply") == "in" else ("CANCELLED" if i.get("reply") in ("not_this_one", "not_this_week") else "TENTATIVE")  # a held plan is TENTATIVE
        venue = o.get("venue", {})
        lines += ["BEGIN:VEVENT", f"UID:{i['invite_id']}@cahoots", f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
                  f"DTSTART;TZID=America/Winnipeg:{start.strftime('%Y%m%dT%H%M%S')}", f"DTEND;TZID=America/Winnipeg:{end.strftime('%Y%m%dT%H%M%S')}",
                  f"SUMMARY:{_ics_escape('Cahoots: ' + venue.get('name', 'a night out'))}",
                  f"LOCATION:{_ics_escape(', '.join(x for x in (venue.get('name', ''), venue.get('address', ''), venue.get('neighbourhood', '')) if x))}",
                  f"DESCRIPTION:{_ics_escape(i['text'] + ((' With: ' + ', '.join(i['guests_preview'])) if i.get('guests_preview') else '') + ' Reply: ' + base + '/me/' + pid)}",
                  f"STATUS:{status}", f"URL:{base}/me/{pid}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
