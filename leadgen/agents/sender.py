"""Outreach Executor agent: sends approved emails.

Three modes, decided by .env (see Settings.sending_mode):
- dry run  : nothing leaves the machine (default)
- test mode: real emails, but every one is redirected to TEST_RECIPIENT
- live     : real emails to the leads; each send is written to the contact history
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Settings
from ..state import PipelineState, event
from ..tools import contacts
from ..tools.mailer import build_mailer

AGENT = "Outreach Executor"
_RESERVED_DOMAINS = (".example", "example.com", "example.org", "example.net", ".test", ".invalid")


def run(state: PipelineState, settings: Settings) -> dict[str, Any]:
    approved = [d for d in state["drafts"] if d["status"] == "approved"]
    mailer = build_mailer(settings, state["campaign"]["seller"]["sender_name"])
    test_to = settings.test_recipient if mailer else ""
    suppressed = contacts.load_suppressed()

    results = []
    for draft in approved:
        base = {"lead_id": draft["lead_id"], "to": draft["to"], "subject": draft["subject"],
                "sent_at": datetime.now().isoformat(timespec="seconds")}
        if mailer is None:
            results.append({**base, "status": "simulated"})
        elif contacts.is_suppressed(draft["to"], suppressed):  # re-checked at the last moment
            results.append({**base, "status": "skipped", "error": "On the do-not-contact list"})
        elif test_to:
            outcome = mailer.send(test_to, f"[TEST - would go to {draft['to']}] {draft['subject']}", draft["body"])
            status = "sent_test" if outcome["status"] == "sent" else "failed"
            results.append({**base, **outcome, "status": status, "delivered_to": test_to})
        elif draft["to"].lower().endswith(_RESERVED_DOMAINS):
            results.append({**base, "status": "skipped", "error": "Reserved demo address"})
        else:
            outcome = mailer.send(draft["to"], draft["subject"], draft["body"])
            results.append({**base, **outcome})
            if outcome["status"] == "sent":
                contacts.record_sent(draft["to"], draft["company"], draft["subject"], state.get("run_id", ""))

    count = lambda status: sum(1 for r in results if r["status"] == status)  # noqa: E731
    if mailer is None:
        message = f"{settings.sending_mode}. Simulated {count('simulated')} sends; no email left this machine."
    elif test_to:
        message = f"TEST MODE: {count('sent_test')} email(s) delivered to {test_to} instead of the leads, {count('failed')} failed."
    else:
        message = f"LIVE: {count('sent')} sent, {count('failed')} failed, {count('skipped')} skipped."
    trace = [event(AGENT, message)]
    for result in results:
        if result["status"] == "failed":
            trace.append(event(AGENT, f"Could not send to {result['to']}: {result.get('error')}", "error"))
    return {"send_results": results, "trace": trace}
