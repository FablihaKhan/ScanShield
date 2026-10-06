"""Step 8: privacy QA gate.

Automatic scan of every de-identified record; any issue blocks the record.
A person can override a blocked record in qa_overrides.csv, and must sign off
a random sample of passing records in qa_human_sample.csv before release.
"""
import csv
import random
import re
from collections import Counter
from pathlib import Path

from .deidentify import CLINICIAN_RE, CREDENTIAL_RE, EMAIL_RE, LONG_NUMBER_RE, PHONE_RE, TEXT_FIELDS
from .ksrl import TITLES

_WORD_RE = re.compile(r"[a-z]{2,}")
_STOP = {"the", "and", "with", "for", "are", "was", "were", "not", "seen", "noted", "both", "left", "right"}
OVERRIDE_COLUMNS = ["report_id", "decision", "reviewer", "note"]
SAMPLE_COLUMNS = ["report_id", "source_file", "exam", "clinical_information", "findings", "impression",
                  "advice", "sex", "age", "reviewer_ok", "reviewer", "note"]


def clinical_vocabulary(bodies: list[str], min_docs: int = 3) -> set[str]:
    df = Counter()
    for b in bodies:
        df.update(set(_WORD_RE.findall(b.lower())))
    return {w for w, n in df.items() if n >= min_docs}


def global_name_tokens(names: list[str], vocab: set[str]) -> set[str]:
    toks = set()
    for n in names:
        toks.update(t for t in _WORD_RE.findall((n or "").lower()) if len(t) >= 4)
    return toks - vocab - TITLES - _STOP


def check_record(staged: dict, own_terms: set[str], id_no: str | None, staff_terms: set[str],
                 other_names: set[str], scrub_counts: dict, parse_warnings: list[str],
                 linkage_pending: bool, block_pending: bool) -> list[str]:
    issues = []
    body = "\n".join(staged.get(f) or "" for f in TEXT_FIELDS)
    low = body.lower()
    words = set(_WORD_RE.findall(low))
    hits = sorted(t for t in own_terms if len(t) >= 3 and t in words)
    if hits:
        issues.append("own_name_token:" + ",".join(hits))
    if id_no and len(id_no.strip()) >= 3 and re.search(rf"\b{re.escape(id_no.strip())}\b", body):
        issues.append("id_no_in_text")
    for label, rx in (("phone", PHONE_RE), ("email", EMAIL_RE), ("clinician", CLINICIAN_RE),
                      ("credential", CREDENTIAL_RE), ("long_number", LONG_NUMBER_RE)):
        if rx.search(body):
            issues.append(f"{label}_pattern")
    staff_hits = sorted(t for t in staff_terms if t in words)
    if staff_hits:
        issues.append("metadata_person_token:" + ",".join(staff_hits))
    other = sorted((words & other_names) - own_terms)
    if other:
        issues.append("possible_name_token:" + ",".join(other[:5]))
    if scrub_counts.get("name"):
        issues.append("patient_name_was_in_body_check_scrub")
    for w in parse_warnings:
        if w in ("no_header", "no_impression", "extraction_failed"):
            issues.append(f"parse:{w}")
    if not (staged.get("findings") or staged.get("impression")):
        issues.append("missing_clinical_content")
    if linkage_pending and block_pending:
        issues.append("linkage_pending_review")
    return issues


def load_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=OVERRIDE_COLUMNS).writeheader()
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["report_id"]: (r.get("decision") or "").strip().lower() for r in csv.DictReader(f)}


def write_human_sample(path: Path, passed: list[dict], sources: dict[str, str],
                       fraction: float, minimum: int, seed: int | None = None) -> int:
    """Create the sign-off sample once; later runs keep the reviewer's answers."""
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    n = min(len(passed), max(minimum, round(len(passed) * fraction)))
    sample = random.Random(seed).sample(passed, n) if n else []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SAMPLE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for s in sample:
            w.writerow({**s, "source_file": sources[s["report_id"]], "reviewer_ok": "", "reviewer": "", "note": ""})
    return n


def read_human_sample(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))
