"""Response Tracker agent: engagement per lead.

- Dry run: there is no real engagement, so it is SIMULATED (seeded, loosely tied
  to lead score) purely so the feedback agent has something to analyse.
- Real sends: nobody has had time to open anything yet. Opens and clicks are
  only filled in if SendGrid webhook events have been recorded by
  webhook_server.py; replies arrive in the sender's inbox and are not read here.
The result is always labelled with its source and whether it was measured.
"""

from __future__ import annotations

import json
import random
from typing import Any

from ..config import ROOT
from ..state import PipelineState, event

AGENT = "Response Tracker"
EVENTS_FILE = ROOT / "tracking_events.json"


def _simulate(lead_id: str, score: float) -> dict[str, bool]:
    rng = random.Random(lead_id)  # same lead -> same outcome, so demos are repeatable
    opened = rng.random() < 0.20 + 0.30 * score / 100
    clicked = opened and rng.random() < 0.35
    replied = opened and rng.random() < 0.05 + 0.20 * score / 100
    return {"opened": opened, "clicked": clicked, "replied": replied, "meeting": replied and rng.random() < 0.5}


def _webhook_events() -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    return json.loads(EVENTS_FILE.read_text(encoding="utf-8")).get("events", [])


def run(state: PipelineState) -> dict[str, Any]:
    scores = {item["lead_id"]: item for item in state["scored"]}
    results = state.get("send_results", [])
    delivered = [r for r in results if r["status"] in ("sent", "sent_test", "simulated")]
    simulated = bool(delivered) and all(r["status"] == "simulated" for r in delivered)
    events = [] if simulated else _webhook_events()

    responses = []
    for result in delivered:
        item = scores[result["lead_id"]]
        if simulated:
            outcome = _simulate(result["lead_id"], item["score"])
        else:
            kinds = {e.get("event") for e in events if (e.get("email") or "").lower() == result["to"].lower()}
            outcome = {"opened": "open" in kinds, "clicked": "click" in kinds, "replied": False, "meeting": False}
        responses.append(
            {"lead_id": result["lead_id"], "company": item["lead"]["company"],
             "category": item["lead"].get("category"), "score": item["score"],
             "digital_maturity": item["lead"]["signals"]["digital_maturity"], **outcome}
        )

    total = len(responses)
    rate = lambda key: round(sum(r[key] for r in responses) / total, 3) if total else 0.0  # noqa: E731
    measured = not simulated and any(r["opened"] or r["clicked"] for r in responses)
    engagement = {
        "source": "simulated" if simulated else "live",
        "measured": measured,
        "delivered": total,
        "open_rate": rate("opened"),
        "click_rate": rate("clicked"),
        "reply_rate": rate("replied"),
        "meeting_rate": rate("meeting"),
        "responses": responses,
    }
    if simulated:
        message = (f"SIMULATED engagement (dry run): {engagement['open_rate']:.0%} opened, "
                   f"{engagement['reply_rate']:.0%} replied across {total} emails.")
    else:
        message = (f"{total} real email(s) sent. Engagement is not known yet: replies will arrive in the "
                   "sender's inbox, and opens/clicks need the SendGrid webhook.")
    return {"engagement": engagement, "trace": [event(AGENT, message)]}
