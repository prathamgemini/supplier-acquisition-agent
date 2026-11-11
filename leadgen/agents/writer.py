"""Outreach Writer agent: first drafts, then revisions driven by the critic."""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLM
from ..tools.lead_sources import looks_like_name
from ..state import EmailDraft, PipelineState, event, parallel_map, progress_writer

AGENT = "Outreach Writer"
OPT_OUT = "If this isn't relevant, just reply \"no\" and I won't follow up."

SYSTEM = """You write first-touch B2B emails for {sender_name}, {sender_role} at {seller} - {description}.
Tone: {tone}.
Rules:
- Maximum {max_words} words in the body.
- Open with one specific, true fact about the recipient's business. No flattery.
- Explain the offer in terms of their situation: {offer}
- One call to action only: {cta}
- Never invent numbers, customers, guarantees or facts that are not in the lead record.
- Do not quote the recipient's own turnover, staff count or GST status back to them.
- Plain text. No placeholders, no markdown, no emojis.
- The first line of the body must be exactly this greeting: {greeting}
- Use only fields that are present in the lead record.
- End with the sign-off, then this exact line: {opt_out}

Output format: the first line is "Subject: <subject line under 60 characters>", then a blank line,
then the complete email body. Output nothing else."""

USER_NEW = """Lead record:
{lead}

Analyst notes:
- Pain points: {pains}
- Personalisation hook: {hook}
- Likely objection (pre-empt it lightly): {objection}

Write the email."""

USER_REVISE = """Your previous draft was reviewed and needs changes.

Previous subject: {subject}
Previous body:
{body}

Reviewer issues: {issues}
Reviewer instructions: {instructions}

Lead record:
{lead}

Rewrite the email, fixing every issue."""


_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "prof", "er", "ca"}
_GREETING_LINE = re.compile(r"^(hi|hello|hey|dear|namaste|greetings|respected|good (morning|afternoon|evening))\b", re.I)


def greeting(lead: dict[str, Any]) -> str:
    """Always address the recipient by name: the person if known, otherwise the business."""
    name = lead.get("contact_name") or ""
    if not looks_like_name(name):  # e.g. a phone number that ended up in the name field
        name = ""
    words = [w for w in name.replace(".", " ").split() if w.lower() not in _HONORIFICS and w[0].isalpha()]
    return f"Hi {words[0].title() if words[0].islower() else words[0]}," if words else f"Hi {lead['company']} team,"


def with_greeting(body: str, greeting_line: str) -> str:
    """Make `greeting_line` the first line, replacing whatever salutation the model wrote."""
    lines = body.strip().splitlines()
    if lines and _GREETING_LINE.match(lines[0].strip()) and len(lines[0].split()) <= 8:
        lines = lines[1:]
    return greeting_line + "\n\n" + "\n".join(lines).strip()


def _template(item: dict[str, Any], campaign: dict[str, Any]) -> EmailDraft:
    lead, seller = item["lead"], campaign["seller"]
    signal = (lead.get("recent_signal") or "").rstrip(".")
    opener = (
        f"I noticed {lead['company']} recently {signal[0].lower()}{signal[1:]}."
        if signal
        else f"I came across {lead['company']} while looking at {lead.get('category') or 'suppliers'} in {lead.get('city') or 'your area'}."
    )
    body = (
        f"{greeting(lead)}\n\n"
        f"{opener}\n\n"
        f"{seller['company']} is {seller['description']}. For {lead['company']}, that would mean "
        f"{seller['offer']}.\n\n"
        f"Would you be open to {seller['cta']}?\n\n"
        f"Regards,\n{seller['sender_name']}\n{seller['sender_role']}, {seller['company']}\n\n"
        f"{OPT_OUT}"
    )
    return EmailDraft(subject=f"Buyer enquiries for {lead['company']}", body=body)


def _parse(text: str | None, greeting_line: str) -> EmailDraft | None:
    """Split 'Subject: ...' + body; guarantee the named greeting and the opt-out line."""
    if not text:
        return None
    lines = text.strip().splitlines()
    if not lines[0].lower().startswith("subject:"):
        return None
    subject = lines[0].split(":", 1)[1].strip().strip('"')
    body = "\n".join(lines[1:]).strip().replace("’", "'")
    if not subject or len(body.split()) < 25:
        return None
    body = with_greeting(body, greeting_line)
    if "won't follow up" not in body.lower():  # compliance is enforced in code, not left to the model
        body = f"{body}\n\n{OPT_OUT}"
    return EmailDraft(subject=subject, body=body)


def _lead_view(lead: dict[str, Any]) -> str:
    hidden = {"signals", "lead_id", "email", "phone"}
    return json.dumps({k: v for k, v in lead.items() if k not in hidden}, ensure_ascii=False)


def write_draft(
    item: dict[str, Any], campaign: dict[str, Any], llm: LLM, previous: dict[str, Any] | None, emit=lambda _m: None
) -> dict[str, Any]:
    lead, seller, outreach = item["lead"], campaign["seller"], campaign["outreach"]
    system = SYSTEM.format(
        seller=seller["company"], opt_out=OPT_OUT, tone=outreach["tone"], max_words=outreach["max_words"],
        greeting=greeting(lead),
        **{k: seller[k] for k in ("sender_name", "sender_role", "description", "offer", "cta")},
    )
    if previous:
        critique = previous["critique"]
        user = USER_REVISE.format(
            subject=previous["subject"], body=previous["body"], lead=_lead_view(lead),
            issues="; ".join(critique["issues"]) or "none listed",
            instructions=critique["revision_instructions"] or "Tighten and personalise.",
        )
    else:
        user = USER_NEW.format(
            lead=_lead_view(lead), pains="; ".join(item["pain_points"]),
            hook=item["personalization_hook"], objection=item["likely_objection"],
        )

    draft = _parse(llm.text(system, user, temperature=0.7, max_tokens=350), greeting(lead))
    note = f"{'Rewrote' if previous else 'Drafted'} email for {lead['company']}"
    engine = "llm"
    if draft is None:
        draft, engine = _template(item, campaign), "template"

    history = list(previous["history"]) if previous else []
    if previous:
        history.append(
            {
                "subject": previous["subject"],
                "body": previous["body"],
                "quality": previous["quality"],
                "issues": previous["critique"]["issues"],
            }
        )
    emit(note)
    return {
        "lead_id": item["lead_id"],
        "company": lead["company"],
        "contact": lead["contact_name"],
        "to": lead.get("email") or "",
        "score": item["score"],
        "subject": draft.subject.strip(),
        "body": draft.body.strip(),
        "engine": engine,
        "status": "drafted",
        "revisions": len(history),
        "history": history,
        "critique": None,
        "quality": None,
        "flags": [],
    }


def run(state: PipelineState, llm: LLM) -> dict[str, Any]:
    campaign = state["campaign"]
    by_id = {item["lead_id"]: item for item in state.get("scored", [])}
    existing = state.get("drafts") or []
    emit = progress_writer()
    workers = getattr(llm, "concurrency", 4)

    if not existing:
        limit = campaign["outreach"]["max_emails_per_run"]
        targets = [item for item in state["scored"] if item["tier"] == "qualified"][:limit]
        drafts = parallel_map(lambda item: write_draft(item, campaign, llm, None, emit), targets, workers)
        message = f"Drafted {len(drafts)} personalised emails for qualified leads."
        return {"drafts": drafts, "revision_round": 0, "trace": [event(AGENT, message)]}

    to_revise = [d for d in existing if d["status"] == "needs_revision"]
    rewrites = parallel_map(lambda d: write_draft(by_id[d["lead_id"]], campaign, llm, d, emit), to_revise, workers)
    revised = {d["lead_id"]: d for d in rewrites}
    drafts = [revised.get(d["lead_id"], d) for d in existing]
    round_no = state.get("revision_round", 0) + 1
    names = ", ".join(d["company"] for d in to_revise)
    message = f"Revision round {round_no}: rewrote {len(revised)} draft(s) using critic feedback ({names})."
    return {"drafts": drafts, "revision_round": round_no, "trace": [event(AGENT, message)]}
