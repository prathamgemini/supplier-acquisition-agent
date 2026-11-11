"""Settings (from .env) and campaign configuration (from workflow.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "qwen/qwen3.8-27b"
_PLACEHOLDER_PREFIXES = ("your", "paste", "gsk_xxx", "<")


def _secret(name: str) -> str:
    """Read a value from the environment, treating template placeholders as unset."""
    value = (os.getenv(name) or "").strip().strip('"').strip("'")
    if not value or value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return value


@dataclass(frozen=True)
class Settings:
    groq_api_key: str
    groq_model: str
    dry_run: bool
    sendgrid_api_key: str
    from_email: str
    reasoning: str = "none"  # Qwen thinking depth for analyst/critic calls: none | low | medium | high
    llm_concurrency: int = 2
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    test_recipient: str = ""  # when set, every live email is redirected to this address

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def email_provider(self) -> str:
        """Which delivery service is configured: 'smtp', 'sendgrid' or '' (none)."""
        if self.smtp_host:
            return "smtp"
        return "sendgrid" if self.sendgrid_api_key else ""

    @property
    def can_send_live(self) -> bool:
        return not self.dry_run and bool(self.email_provider and self.from_email)

    @property
    def sending_mode(self) -> str:
        """One-line, human-readable description of what 'send' will do."""
        if self.can_send_live:
            via = "Gmail/SMTP" if self.email_provider == "smtp" else "SendGrid"
            if self.test_recipient:
                return f"TEST MODE via {via}: every email is redirected to {self.test_recipient}"
            return f"LIVE via {via}, from {self.from_email}"
        if not self.dry_run:
            return "Dry run (ENABLE_DRY_RUN is off, but no email service is configured)"
        return "Dry run: emails are never actually sent"


def get_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=True)
    smtp_user = _secret("SMTP_USER")
    return Settings(
        groq_api_key=_secret("GROQ_API_KEY"),
        groq_model=(os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip(),
        dry_run=(os.getenv("ENABLE_DRY_RUN", "true").strip().lower() != "false"),
        sendgrid_api_key=_secret("SENDGRID_API_KEY"),
        from_email=_secret("FROM_EMAIL") or _secret("SENDGRID_FROM_EMAIL") or smtp_user,
        reasoning=(os.getenv("GROQ_REASONING") or "none").strip().lower(),
        llm_concurrency=max(1, int(os.getenv("LLM_CONCURRENCY") or 2)),
        smtp_host=_secret("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT") or 587),
        smtp_user=smtp_user,
        smtp_password=_secret("SMTP_PASSWORD"),
        test_recipient=_secret("TEST_RECIPIENT"),
    )


def validate_campaign(campaign: dict[str, Any]) -> None:
    """Fail fast with a readable message if workflow.json is inconsistent."""
    for section in ("seller", "icp", "scoring", "outreach"):
        if section not in campaign:
            raise ValueError(f"workflow.json is missing the '{section}' section")

    scoring = campaign["scoring"]
    total = sum(scoring["weights"].values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"scoring.weights must sum to 1.0 (got {total:.2f})")
    if not 0 <= scoring["llm_weight"] <= 1:
        raise ValueError("scoring.llm_weight must be between 0 and 1")
    if scoring["nurture_min"] >= scoring["qualified_min"]:
        raise ValueError("scoring.nurture_min must be lower than scoring.qualified_min")

    for band in ("employee_count", "annual_turnover_lakh"):
        lo, hi = campaign["icp"][band]["min"], campaign["icp"][band]["max"]
        if lo <= 0 or lo >= hi:
            raise ValueError(f"icp.{band} needs 0 < min < max")


def load_campaign(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else ROOT / "workflow.json"
    campaign = json.loads(path.read_text(encoding="utf-8"))
    validate_campaign(campaign)
    return campaign
