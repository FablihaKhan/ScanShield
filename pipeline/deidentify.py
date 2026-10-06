"""Steps 4 and 6: scrub identifiers from report text, shift dates, coarsen age.

Only the parsed clinical sections are ever written downstream; the .doc file
itself (with its Word metadata, file name and signature appendix) never leaves
the raw folder, so metadata/revision data is removed by construction.
"""
import re
from datetime import date, timedelta

from .parse_report import parse_date

CLINICIAN_RE = re.compile(r"\b(?:dr|prof|professor)\b\.?\s*(?:[A-Z][\w.'-]*\s*){1,4}", re.I)
CREDENTIAL_RE = re.compile(r"\b(?:MBBS|FCPS|MCPS|DMRD|MRCR|RDMS|M\.?\s?Phil)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+?88[\s-]?)?\b01[3-9]\d{2}[\s-]?\d{6}\b")
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
DATE_IN_TEXT_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?[\s\-/.,]*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/.,]*\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{1,2}[./\-]\d{1,2}[./\-](?:\d{4}|\d{2})\b|\b\d{4}-\d{1,2}-\d{1,2}\b",
    re.I)

TEXT_FIELDS = ("exam", "clinical_information", "technique", "findings", "impression", "advice")


def age_output(age_years: float | None) -> tuple[str, str]:
    """(age, age_band) with ages >= 90 collapsed to '90+' (HIPAA Safe Harbor rule)."""
    if age_years is None:
        return "", ""
    a = int(age_years)
    if a >= 90:
        return "90+", "90+"
    lo = a - a % 5
    return str(a), f"{lo}-{lo + 4}"


def shift(d: date | None, days: int) -> date | None:
    return d + timedelta(days=days) if d else None


def _name_regex(terms: set[str]):
    terms = sorted((t for t in terms if len(t) >= 2), key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + r")\b", re.I)


def scrub_text(text: str, name_terms: set[str], id_no: str | None, shift_days: int) -> tuple[str, dict]:
    """Returns (scrubbed text, counts of each replacement type)."""
    counts = {}
    if not text:
        return "", counts

    def sub(pattern, repl, s, label):
        s2, n = pattern.subn(repl, s)
        if n:
            counts[label] = counts.get(label, 0) + n
        return s2

    text = sub(CLINICIAN_RE, "[CLINICIAN]", text, "clinician")
    text = sub(CREDENTIAL_RE, "[CLINICIAN]", text, "clinician")
    text = sub(EMAIL_RE, "[EMAIL]", text, "email")
    text = sub(PHONE_RE, "[PHONE]", text, "phone")
    if id_no and len(id_no.strip()) >= 3:
        text = sub(re.compile(rf"\b{re.escape(id_no.strip())}\b", re.I), "[ID]", text, "id_no")

    def shift_date(m):
        d = parse_date(m.group(0))
        counts["date"] = counts.get("date", 0) + 1
        return shift(d, shift_days).isoformat() if d else "[DATE]"

    text = DATE_IN_TEXT_RE.sub(shift_date, text)
    text = sub(LONG_NUMBER_RE, "[NUMBER]", text, "long_number")
    rx = _name_regex(name_terms)
    if rx:
        text = sub(rx, "[NAME]", text, "name")
    return text, counts
