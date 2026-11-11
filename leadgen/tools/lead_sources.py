"""Lead loaders. Swap these for a CRM / data-provider API without touching the agents.

The CSV importer is deliberately forgiving, because real lead lists are messy:
- only a business-name column is required;
- column names are matched loosely ("Company Name", "Mobile No", "Industry" ... all work);
- a blank cell means "unknown" and is never counted against a lead;
- columns it does not recognise are kept and shown to the LLM as extra context.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ROOT

# (field, kind, what to put in it, other header names that are accepted)
FIELDS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("company", "text", "Business name. The only required column.",
     ("company name", "business name", "business", "firm", "firm name", "organisation", "organization",
      "supplier", "supplier name", "account", "account name")),
    ("contact_name", "text", "Person the email is addressed to.",
     ("contact", "contact person", "name", "owner", "owner name", "person", "person name", "full name", "poc",
      "first name", "customer name", "client name", "lead name", "proprietor", "proprietor name",
      "director name", "concerned person", "key person", "spoc")),
    ("email", "text", "Needed to send an email. Without it the lead is kept for calling instead.",
     ("email id", "e mail", "mail", "mail id", "email address")),
    ("phone", "text", "Any phone or WhatsApp number.",
     ("mobile", "mobile no", "mobile number", "phone no", "phone number", "contact number", "contact no", "whatsapp")),
    ("title", "text", "Job title, e.g. Owner, Director, Sales Manager.",
     ("designation", "role", "position", "job title")),
    ("city", "text", "City or town.", ("town", "location", "place")),
    ("state", "text", "State.", ("region", "province")),
    ("category", "text", "What kind of business it is, e.g. packaging, food processing.",
     ("industry", "sector", "business type", "business category", "segment", "vertical", "type")),
    ("products", "list", "Main products, separated by ; (semicolon).",
     ("product", "product list", "items", "offerings", "what they sell")),
    ("employee_count", "number", "Number of employees.",
     ("employees", "no of employees", "number of employees", "team size", "staff", "headcount", "company size")),
    ("annual_turnover_lakh", "number", "Yearly turnover in Rs lakh. Plain number, or with a unit: 80 lakh, 7 Cr.",
     ("turnover", "annual turnover", "turnover lakh", "turnover in lakh", "turnover lakhs", "revenue", "sales")),
    ("years_in_business", "number", "Years the business has been running.",
     ("years", "years in operation", "age of business", "experience")),
    ("gst_registered", "yes/no", "yes or no. A GSTIN number also counts as yes.",
     ("gst", "gstin", "gst no", "gst number", "gst registration")),
    ("online_presence", "list",
     "Where they are online, separated by ; e.g. website;linkedin;instagram. Write none if they have nothing.",
     ("website", "online", "online channels", "channels", "web presence", "digital presence", "social media")),
    ("export_markets", "list", "Countries they export to, separated by ;", ("exports", "export", "export countries")),
    ("recent_signal", "text", "One sentence of recent news about the business. Makes the email personal.",
     ("news", "recent news", "notes", "note", "remarks", "comments", "description", "about", "update", "signal")),
]

_NONE_WORDS = {"none", "no", "nil", "na", "n/a", "-", "nothing"}
_YES = {"yes", "y", "true", "1", "registered"}
_NO = {"no", "n", "false", "0", "unregistered", "not registered"}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (header or "").lower()).strip()


_ALIASES = {_norm(alias): name for name, _, _, aliases in FIELDS for alias in (name, *aliases)}
_KINDS = {name: kind for name, kind, _, _ in FIELDS}


# Columns whose header-based guess may be overridden by what the cells actually contain.
_SNIFFABLE = {None, "contact_name", "company", "title", "city", "state", "email", "phone"}


def _is_phone(value: str) -> bool:
    return bool(re.fullmatch(r"[+()\d\s./-]{7,}", value)) and sum(c.isdigit() for c in value) >= 7


def looks_like_name(value: str) -> bool:
    """True for something a person or business could be called: has letters, is not a number or an address."""
    value = (value or "").strip()
    letters = sum(c.isalpha() for c in value)
    return letters >= 2 and "@" not in value and not _is_phone(value) and sum(c.isdigit() for c in value) < letters


def _sniff(values: list[str]) -> str | None:
    """'phone' or 'email' if at least 70% of the non-blank cells look like one, else None."""
    filled = [v for v in values if v]
    if not filled:
        return None
    if sum(1 for v in filled if _is_phone(v)) >= 0.7 * len(filled):
        return "phone"
    if sum(1 for v in filled if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v)) >= 0.7 * len(filled):
        return "email"
    return None


@dataclass
class ImportResult:
    leads: list[dict[str, Any]]
    mapped: dict[str, str] = field(default_factory=dict)  # our field -> the file's column header
    extra: list[str] = field(default_factory=list)  # unrecognised columns, kept as context
    missing: list[str] = field(default_factory=list)  # fields with no matching column
    skipped_rows: int = 0  # rows with no business name

    @property
    def usable(self) -> bool:
        return "company" in self.mapped and bool(self.leads)


def _parse_list(value: str, field_name: str) -> list[str] | None:
    if not value:
        return None  # unknown
    if value.lower() in _NONE_WORDS:
        return []  # explicitly nothing
    items = [item.strip() for item in re.split(r"[;|,]", value) if item.strip()]
    if field_name == "online_presence":
        looks_like_site = re.compile(r"^(https?://|www\.)|\.[a-z]{2,}(/|$)", re.I)
        items = ["website" if looks_like_site.search(item) else item.lower().replace(" ", "_") for item in items]
    return list(dict.fromkeys(items))


def _parse_number(value: str, field_name: str = "") -> int | None:
    match = re.search(r"\d+(\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    if field_name == "annual_turnover_lakh":  # stored in Rs lakh; accept "7 Cr", "2.5 crore", "80 lakh", "50L"
        unit = value[match.end():].strip().lower()
        if unit.startswith("cr"):
            number *= 100
        elif unit.startswith(("k", "thousand")):
            number /= 100
    return int(number)


def _parse_yes_no(value: str) -> bool | None:
    text = value.strip().lower()
    if text in _YES or re.fullmatch(r"[0-9a-z]{15}", text):  # a 15-character GSTIN
        return True
    if text in _NO:
        return False
    return None


def import_csv(text: str) -> ImportResult:
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
    rows = list(reader)

    # Headers can mislead ("Contact" may hold phone numbers), so look at what each column contains.
    content = {header: _sniff([(row.get(header) or "").strip() for row in rows]) for header in headers}
    mapped: dict[str, str] = {}
    extra: list[str] = []
    undecided: list[str] = []
    for header in headers:
        by_name = _ALIASES.get(_norm(header))
        kind = content[header] if by_name in _SNIFFABLE else None
        if kind and kind not in mapped:
            mapped[kind] = header  # a column full of phone numbers IS the phone column, whatever it is called
        elif kind:
            extra.append(header)
        else:
            undecided.append(header)
    for header in undecided:
        name = _ALIASES.get(_norm(header))
        if name and name not in mapped:
            mapped[name] = header
        else:
            extra.append(header)
    # A lone "Name" column in a list of businesses is the business name.
    if "company" not in mapped and "contact_name" in mapped:
        mapped["company"] = mapped.pop("contact_name")

    leads: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        cell = lambda header: (row.get(header) or "").strip()  # noqa: E731
        if not cell(mapped.get("company", "")):
            skipped += 1
            continue
        lead: dict[str, Any] = {}
        for name, header in mapped.items():
            value, kind = cell(header), _KINDS[name]
            if kind == "list":
                parsed: Any = _parse_list(value, name)
            elif kind == "number":
                parsed = _parse_number(value, name)
            elif kind == "yes/no":
                parsed = _parse_yes_no(value)
            else:
                parsed = value or None
            if parsed is not None:
                lead[name] = parsed
        # A "name" that is really a number or an address is not a name: move it or drop it.
        for key in ("contact_name", "company"):
            value = lead.get(key, "")
            if value and not looks_like_name(value):
                del lead[key]
                if _is_phone(value):
                    lead.setdefault("phone", value)
                elif "@" in value:
                    lead.setdefault("email", value)
        if "company" not in lead:
            skipped += 1
            continue
        for header in extra:  # keep unrecognised columns as free-text context
            if cell(header):
                lead[header.strip()] = cell(header)
        leads.append(lead)

    missing = [name for name, _, _, _ in FIELDS if name not in mapped]
    return ImportResult(leads, mapped, extra, missing, skipped)


def parse_csv(text: str) -> list[dict[str, Any]]:
    return import_csv(text).leads


def decode(data: bytes) -> str:
    """Excel on Windows often saves CSV as cp1252 rather than UTF-8."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def template_csv() -> str:
    """A ready-to-fill CSV with every supported column and two example rows."""
    rows = [
        {"company": "Acme Pumps", "contact_name": "Ravi Kumar", "email": "ravi@acmepumps.example",
         "phone": "+91-90000-00000", "title": "Owner", "city": "Rajkot", "state": "Gujarat",
         "category": "industrial machinery", "products": "submersible pumps;motors", "employee_count": 80,
         "annual_turnover_lakh": 1500, "years_in_business": 12, "gst_registered": "yes",
         "online_presence": "website", "export_markets": "",
         "recent_signal": "Commissioned a new assembly line and is appointing dealers in Maharashtra."},
        {"company": "Lakshmi Spices", "contact_name": "", "email": "", "phone": "+91-90000-00001", "title": "",
         "city": "Erode", "state": "", "category": "food processing", "products": "", "employee_count": "",
         "annual_turnover_lakh": "", "years_in_business": "", "gst_registered": "", "online_presence": "none",
         "export_markets": "", "recent_signal": ""},
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[name for name, _, _, _ in FIELDS], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def load_json(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["leads"] if isinstance(data, dict) else data
