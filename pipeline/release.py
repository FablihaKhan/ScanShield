"""Steps 9-10: build the research dataset from QA-passed records only.

Refuses to run until every row of qa_human_sample.csv has reviewer_ok = yes,
re-scans the outgoing text once more, and reports small cells (k-anonymity on
sex x age band x modality) that must be suppressed or merged before any
table/figure is published.
"""
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .deidentify import CLINICIAN_RE, CREDENTIAL_RE, EMAIL_RE, LONG_NUMBER_RE, PHONE_RE, TEXT_FIELDS
from .qa import read_human_sample

REPORT_COLUMNS = ["research_patient_id", "encounter_id", "report_id", "study_id", "image_available",
                  "report_seq", "days_since_first_report", "report_date_shifted", "sex", "age", "age_band",
                  "modality", "contrast", "body_region", *TEXT_FIELDS]


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def release(secure_dir: Path, research_dir: Path, k_min: int) -> int:
    staging_path = secure_dir / "staging_deid.jsonl"
    if not staging_path.exists():
        print("No staging data. Run `python -m pipeline run` first.")
        return 1
    staged = [json.loads(l) for l in staging_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    sample = read_human_sample(secure_dir / "qa_human_sample.csv")
    not_ok = [r for r in sample if (r.get("reviewer_ok") or "").strip().lower() != "yes"]
    if not sample or not_ok:
        print(f"Release blocked: human QA sample has {len(not_ok)} of {len(sample)} rows not marked "
              f"reviewer_ok=yes in {secure_dir / 'qa_human_sample.csv'}.")
        print("Fix the scrubbing for any 'no' row, delete the sample file, re-run `run`, and review again.")
        return 1

    passed = [s for s in staged if s["qa_status"] in ("passed", "passed_override")]
    leaks = []
    for s in passed:
        body = "\n".join(s.get(f) or "" for f in TEXT_FIELDS)
        for rx in (PHONE_RE, EMAIL_RE, CLINICIAN_RE, CREDENTIAL_RE, LONG_NUMBER_RE):
            if rx.search(body):
                leaks.append(s["report_id"])
                break
    if leaks:
        print(f"Release blocked: final scan found identifier patterns in {len(leaks)} records: {leaks[:10]}")
        return 1

    research_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(research_dir / "reports.csv", passed, REPORT_COLUMNS)
    with open(research_dir / "reports.jsonl", "w", encoding="utf-8") as f:
        for s in passed:
            f.write(json.dumps({c: s.get(c) for c in REPORT_COLUMNS}, ensure_ascii=False) + "\n")

    patients = defaultdict(list)
    for s in passed:
        patients[s["research_patient_id"]].append(s)
    prow = [{"research_patient_id": pid, "sex": rs[0]["sex"], "n_reports": len(rs),
             "first_age_band": min(rs, key=lambda r: r["report_seq"])["age_band"],
             "follow_up_days": max(r["days_since_first_report"] or 0 for r in rs),
             "modalities": ";".join(sorted({r["modality"] for r in rs}))}
            for pid, rs in patients.items()]
    _write_csv(research_dir / "patients.csv", prow, list(prow[0]) if prow else ["research_patient_id"])

    # step 2 inventory, research-safe view
    inv = [{"report_id": s["report_id"], "modality": s["modality"], "contrast": s["contrast"],
            "body_region": s["body_region"], "image_available": s["image_available"]} for s in passed]
    _write_csv(research_dir / "inventory.csv", inv, ["report_id", "modality", "contrast", "body_region", "image_available"])

    # step 9: cohort / data-quality summary
    summary = []
    for dim in ("modality", "contrast", "sex", "age_band", "body_region"):
        for val, n in sorted(Counter(s[dim] or "missing" for s in passed).items()):
            summary.append({"dimension": dim, "value": val, "n_reports": n})
    for f in TEXT_FIELDS:
        summary.append({"dimension": "empty_field", "value": f,
                        "n_reports": sum(1 for s in passed if not s.get(f))})
    _write_csv(research_dir / "cohort_summary.csv", summary, ["dimension", "value", "n_reports"])

    # step 10: small cells on quasi-identifiers (count distinct patients)
    cells = defaultdict(set)
    for s in passed:
        cells[(s["sex"], s["age_band"], s["modality"])].add(s["research_patient_id"])
    small = [{"sex": k[0], "age_band": k[1], "modality": k[2], "n_patients": len(v)}
             for k, v in sorted(cells.items()) if len(v) < k_min]
    _write_csv(research_dir / "small_cells.csv", small, ["sex", "age_band", "modality", "n_patients"])

    manifest = {"released_at": datetime.now().isoformat(timespec="seconds"),
                "n_reports": len(passed), "n_patients": len(patients),
                "n_blocked_by_qa": len(staged) - len(passed), "human_sample_size": len(sample),
                "k_min": k_min, "n_small_cells": len(small),
                "note": "Do not publish rows/figures from small_cells.csv groups without merging or suppression."}
    (research_dir / "release_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0
