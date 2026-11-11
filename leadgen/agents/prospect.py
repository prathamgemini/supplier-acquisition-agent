"""Prospect Search agent: load leads, validate, de-duplicate."""

from __future__ import annotations

import re
from typing import Any

from ..state import PipelineState, event
from ..tools.lead_sources import load_json, looks_like_name

AGENT = "Prospect Search"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def slug(text: str) -> str:
    text = text.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def run(state: PipelineState) -> dict[str, Any]:
    campaign = state["campaign"]
    if state.get("raw_leads"):
        raw, source = state["raw_leads"], "uploaded file"
    else:
        source = campaign["lead_source"]["path"]
        raw = load_json(source)

    leads: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw:
        company = (item.get("company") or "").strip()
        if not company:
            rejects.append({"company": "(blank)", "reason": "Missing business name"})
            continue
        lead_id = slug(company)
        # The same business often arrives twice with slightly different spellings.
        dedupe_key = (item.get("email") or "").lower() or lead_id
        if lead_id in seen or dedupe_key in seen:
            rejects.append({"company": company, "reason": "Duplicate record"})
            continue
        seen.update({lead_id, dedupe_key})
        contact = (item.get("contact_name") or "").strip()
        if not looks_like_name(contact):
            contact = ""
        leads.append({**item, "company": company, "contact_name": contact, "lead_id": lead_id})

    trace = [event(AGENT, f"Loaded {len(raw)} records from {source}; kept {len(leads)} unique leads.")]
    for reject in rejects:
        trace.append(event(AGENT, f"Dropped {reject['company']}: {reject['reason']}.", "warn"))
    return {"leads": leads, "intake_rejects": rejects, "trace": trace}
