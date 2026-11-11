"""The pipeline's memory between runs: who has been emailed, and who must never be.

Stored as two small JSON files in ./storage (git-ignored). For a team or a
larger list, replace this module with your CRM.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import ROOT


def _dir() -> Path:
    folder = Path(os.getenv("LEADGEN_STORAGE") or ROOT / "storage")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read(name: str) -> dict[str, Any]:
    path = _dir() / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(name: str, data: dict[str, Any]) -> None:
    (_dir() / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---- contact history ---------------------------------------------------------
def load_history() -> dict[str, dict[str, Any]]:
    """{email: {company, subject, sent_at, run_id, times}} for every real email sent."""
    return _read("contact_history.json")


def record_sent(email: str, company: str, subject: str, run_id: str) -> None:
    history = load_history()
    key = email.strip().lower()
    history[key] = {
        "company": company,
        "subject": subject,
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "times": history.get(key, {}).get("times", 0) + 1,
    }
    _write("contact_history.json", history)


def recently_contacted(email: str, history: dict[str, dict[str, Any]], within_days: int) -> str | None:
    """Return the date of the last email if it was sent within `within_days`, else None."""
    record = history.get((email or "").strip().lower())
    if not record:
        return None
    sent = datetime.fromisoformat(record["sent_at"])
    return sent.date().isoformat() if datetime.now() - sent < timedelta(days=within_days) else None


# ---- do-not-contact list -----------------------------------------------------
def load_suppressed() -> dict[str, dict[str, str]]:
    """{email or @domain: {reason, added_at}}"""
    return _read("do_not_contact.json")


def add_suppressed(entry: str, reason: str = "Asked not to be contacted") -> str | None:
    """Add an email address, or a whole domain written as @example.com. Returns the stored key."""
    key = entry.strip().lower()
    if "@" not in key or " " in key:
        return None
    data = load_suppressed()
    data[key] = {"reason": reason, "added_at": datetime.now().date().isoformat()}
    _write("do_not_contact.json", data)
    return key


def remove_suppressed(entry: str) -> None:
    data = load_suppressed()
    data.pop(entry.strip().lower(), None)
    _write("do_not_contact.json", data)


def is_suppressed(email: str, suppressed: dict[str, Any]) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    return email in suppressed or ("@" + email.split("@")[-1]) in suppressed
