"""Optional: receive SendGrid Event Webhook callbacks for LIVE campaigns.

    python webhook_server.py     # listens on http://127.0.0.1:5000

Point SendGrid's Event Webhook at <public-url>/webhook/sendgrid (e.g. via a
tunnel). Events are appended to tracking_events.json, which the Response
Tracker agent reads when a run was sent live. Not needed for dry runs.

For production, enable SendGrid's signed webhooks and verify the signature
before trusting a payload.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

EVENTS_FILE = Path(__file__).resolve().parent / "tracking_events.json"
app = Flask(__name__)


def load_events() -> list[dict]:
    if EVENTS_FILE.exists():
        return json.loads(EVENTS_FILE.read_text(encoding="utf-8")).get("events", [])
    return []


@app.post("/webhook/sendgrid")
def sendgrid_webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, list) or not payload:
        return jsonify(status="error", message="Expected a non-empty JSON array of events"), 400

    events = load_events()
    received_at = datetime.now().isoformat(timespec="seconds")
    for item in payload:
        if isinstance(item, dict):
            events.append({**item, "received_at": received_at})
    EVENTS_FILE.write_text(json.dumps({"events": events}, indent=2), encoding="utf-8")
    return jsonify(status="success", processed=len(payload))


@app.get("/stats")
def stats():
    events = load_events()
    kinds = ("delivered", "open", "click", "bounce", "spamreport", "unsubscribe")
    return jsonify(
        total_events=len(events),
        counts={kind: sum(1 for e in events if e.get("event") == kind) for kind in kinds},
        unique_opens=len({e.get("email") for e in events if e.get("event") == "open"}),
    )


@app.get("/health")
def health():
    return jsonify(status="healthy")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
