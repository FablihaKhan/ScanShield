"""Command line entry point.

    python -m pipeline run        # steps 2-8: inventory, KSRL linkage, de-identify, QA gate
    python -m pipeline release    # steps 9-10: write research dataset (needs human QA sign-off)
    python -m pipeline dicom --input <folder of .dcm>   # step 5, when images are available

Nothing printed to the console contains names, IDs or report text.
"""
import argparse
import csv
import json
import re
import secrets
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .deidentify import TEXT_FIELDS, age_output, scrub_text, shift
from .extract import extract_all, filename_tokens, find_antiword, folder_date, list_source_files
from .ksrl import generate_pik, significant_tokens
from .linkage import _new_id, assign_research_ids, link_records, load_decisions, write_review_file
from .parse_report import classify_exam, parse_age_years, parse_date, parse_report, parse_sex
from .qa import check_record, clinical_vocabulary, global_name_tokens, load_overrides, write_human_sample
from .vault import Vault

RAW_DEFAULT = Path(__file__).resolve().parent.parent
INVENTORY_COLUMNS = ["file_uid", "status", "folder_date_valid", "size_bytes", "ole_doc", "modality",
                     "contrast", "body_region", "has_image", "meta_has_author", "filename_has_text",
                     "stop_reason", "warnings"]
EMPTY_PARSE = {"title": None, "header": dict.fromkeys(("id_no", "patient_name", "date", "age", "sex", "examined")),
               "sections": dict.fromkeys(("technique", "clinical_information", "findings", "impression", "advice"), ""),
               "warnings": ["extraction_failed"], "stop_reason": "n/a"}


@dataclass
class Config:
    raw_dir: Path
    secure_dir: Path
    research_dir: Path
    key_file: Path
    antiword: str | None = None
    limit: int | None = None
    max_shift_days: int = 365
    block_pending_linkage: bool = True
    human_qa_fraction: float = 0.05
    human_qa_min: int = 30
    k_min: int = 5


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _birth_year(report_date, age):
    if report_date is None or age is None:
        return None
    frac = report_date.timetuple().tm_yday / 365.25
    return int(report_date.year + frac - age - 0.5)


def cmd_run(cfg: Config) -> int:
    t0 = time.time()
    vault = Vault(cfg.secure_dir, cfg.key_file)
    if vault.created_key:
        print(f"[1] NEW master key created at {cfg.key_file}\n"
              f"    Move it to separate protected storage and back it up: without it the mappings cannot be read.")
    antiword = find_antiword(cfg.antiword)
    paths, n_locks = list_source_files(cfg.raw_dir)
    if cfg.limit:
        paths = paths[:cfg.limit]
    print(f"[2] {len(paths)} .doc reports found ({n_locks} Word lock files skipped); extracting ...")
    extracted = extract_all(paths, cfg.raw_dir, antiword)

    # ---- step 2 inventory + parsing + step 3 PIK -------------------------------
    records, inventory, seen = [], [], set()
    for ex in extracted:
        uid = vault.keyed_hash("file", ex["sha256"], 20)
        inv = {"file_uid": uid, "folder_date_valid": folder_date(ex["folder"]) is not None,
               "size_bytes": ex["size"], "ole_doc": ex["is_ole"], "has_image": False,
               "meta_has_author": bool(ex["meta"].get("author") or ex["meta"].get("last_saved_by")),
               "filename_has_text": bool(filename_tokens(ex["path"]))}
        if uid in seen:
            inventory.append({**inv, "status": "duplicate_file"})
            continue
        seen.add(uid)
        parsed = parse_report(ex["text"]) if (not ex["error"] and ex["text"].strip()) else EMPTY_PARSE
        h = parsed["header"]
        fdate, hdate = folder_date(ex["folder"]), parse_date(h["date"])
        rdate = hdate or fdate
        warnings = list(parsed["warnings"])
        if hdate is None:
            warnings.append("header_date_unparsed")
        elif fdate and abs((hdate - fdate).days) > 7:
            warnings.append("header_date_differs_from_folder")
        age, sex = parse_age_years(h["age"]), parse_sex(h["sex"])
        if age is None:
            warnings.append("age_unparsed")
        by = _birth_year(rdate, age)
        pik = generate_pik(vault, h["patient_name"], sex, by)
        id_no = re.sub(r"\s+", "", h["id_no"] or "") or None
        cls = classify_exam(h["examined"] or "", parsed["sections"]["technique"], parsed["title"] or "")
        records.append({"uid": uid, "ex": ex, "parsed": parsed, "warnings": warnings, "report_date": rdate,
                        "age": age, "sex": sex, "birth_year": by, "pik": pik, "id_no": id_no,
                        "id_hash": vault.keyed_hash("idno", id_no) if id_no else None,
                        "name_norm": " ".join(sorted(significant_tokens(h["patient_name"]))),
                        "name_part": pik["name_part"], "n_tokens": pik["n_tokens"], "cls": cls})
        inventory.append({**inv, "status": "parsed" if "extraction_failed" not in warnings else "extraction_failed",
                          **cls, "stop_reason": parsed["stop_reason"], "warnings": ";".join(warnings)})
    _write_csv(cfg.secure_dir / "inventory.csv", inventory, INVENTORY_COLUMNS)
    print(f"[2] inventory written ({len(records)} unique reports, {len(inventory) - len(records)} duplicate files)")

    # ---- step 3 linkage + research IDs ------------------------------------------
    review_path = cfg.secure_dir / "linkage_review.csv"
    decisions = load_decisions(review_path)
    clusters, review, pending = link_records(records, decisions)
    rid_map, retired = assign_research_ids(clusters, vault.load("research_ids", {}))
    report_ids, encounter_ids = vault.load("report_ids", {}), vault.load("encounter_ids", {})
    used = set(report_ids.values()) | set(encounter_ids.values())
    shifts = vault.load("date_shifts", {})
    image_links = vault.load("image_links", {})
    for r in records:
        report_ids.setdefault(r["uid"], _new_id("RR", used))
        encounter_ids.setdefault(r["uid"], _new_id("EN", used))
    for rid in set(rid_map.values()):
        while rid not in shifts or shifts[rid] == 0:
            shifts[rid] = secrets.randbelow(2 * cfg.max_shift_days + 1) - cfg.max_shift_days

    identity = {}
    for r in records:
        h = r["parsed"]["header"]
        identity[r["uid"]] = {"source": r["ex"]["rel"], "id_no": r["id_no"], "patient_name": h["patient_name"],
                              "sex": r["sex"], "age_raw": h["age"],
                              "report_date": r["report_date"].isoformat() if r["report_date"] else None,
                              "pik": r["pik"]["pik"], "masked_name": r["pik"]["masked_name"],
                              "research_patient_id": rid_map[r["uid"]], "report_id": report_ids[r["uid"]],
                              "encounter_id": encounter_ids[r["uid"]]}
    write_review_file(review_path, review, decisions, identity)
    merges = vault.load("id_merges", []) + [{"retired": a, "into": b} for a, b in retired]

    # ---- steps 4, 6, 7: de-identify, shift dates, link hierarchy -------------------
    by_patient = defaultdict(list)
    for r in records:
        by_patient[rid_map[r["uid"]]].append(r)
    seq, first = {}, {}
    for rid, rs in by_patient.items():
        rs.sort(key=lambda r: (r["report_date"] is None, r["report_date"] or 0, report_ids[r["uid"]]))
        first[rid] = next((r["report_date"] for r in rs if r["report_date"]), None)
        for k, r in enumerate(rs, 1):
            seq[r["uid"]] = k

    bodies = ["\n".join(v for v in r["parsed"]["sections"].values() if v) for r in records]
    vocab = clinical_vocabulary(bodies)
    all_names = [r["parsed"]["header"]["patient_name"] or "" for r in records]
    all_names += [" ".join(filename_tokens(r["ex"]["path"])) for r in records]
    other_names = global_name_tokens(all_names, vocab)
    overrides = load_overrides(cfg.secure_dir / "qa_overrides.csv")

    staged_all, issues_rows, issue_counter = [], [], Counter()
    for r in records:
        uid, h, secs = r["uid"], r["parsed"]["header"], r["parsed"]["sections"]
        rid = rid_map[uid]
        sd = shifts[rid]
        own = set(significant_tokens(h["patient_name"]))
        own |= {t for t in filename_tokens(r["ex"]["path"]) if t not in vocab and t not in ("doc", "copy")}
        staff = {t.lower() for v in r["ex"]["meta"].values() for t in re.findall(r"[A-Za-z]{3,}", v)} - vocab
        age, band = age_output(r["age"])
        staged = {"research_patient_id": rid, "encounter_id": encounter_ids[uid], "report_id": report_ids[uid],
                  "study_id": image_links.get(uid, ""), "image_available": uid in image_links,
                  "report_seq": seq[uid],
                  "days_since_first_report": (r["report_date"] - first[rid]).days if r["report_date"] and first[rid] else None,
                  "report_date_shifted": shift(r["report_date"], sd).isoformat() if r["report_date"] else None,
                  "sex": r["sex"], "age": age, "age_band": band, **r["cls"]}
        counts = Counter()
        sources = {"exam": h["examined"], **{f: secs.get(f) for f in TEXT_FIELDS if f != "exam"}}
        for field in TEXT_FIELDS:
            staged[field], c = scrub_text(sources[field] or "", own, r["id_no"], sd)
            counts.update(c)
        issues = check_record(staged, own, r["id_no"], staff, other_names, counts, r["warnings"],
                              uid in pending, cfg.block_pending_linkage)
        decision = overrides.get(staged["report_id"], "")
        if decision == "pass":
            status = "passed_override"
        elif decision == "fail" or issues:
            status = "failed"
        else:
            status = "passed"
        staged["qa_status"], staged["qa_issues"] = status, ";".join(issues)
        staged_all.append(staged)
        for i in issues:
            issue_counter[i.split(":")[0]] += 1
        if issues or decision:
            issues_rows.append({"report_id": staged["report_id"], "source_file": r["ex"]["rel"],
                                "qa_status": status, "issues": ";".join(issues)})

    # ---- step 8: persist + human sample ------------------------------------------
    with open(cfg.secure_dir / "staging_deid.jsonl", "w", encoding="utf-8") as f:
        for s in staged_all:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    _write_csv(cfg.secure_dir / "qa_issues.csv", issues_rows, ["report_id", "source_file", "qa_status", "issues"])
    passed = [s for s in staged_all if s["qa_status"].startswith("passed")]
    src_by_report = {identity[u]["report_id"]: identity[u]["source"] for u in identity}
    n_sample = write_human_sample(cfg.secure_dir / "qa_human_sample.csv", passed, src_by_report,
                                  cfg.human_qa_fraction, cfg.human_qa_min)

    vault.save("identity_map", identity)
    vault.save("research_ids", rid_map)
    vault.save("report_ids", report_ids)
    vault.save("encounter_ids", encounter_ids)
    vault.save("date_shifts", shifts)
    vault.save("id_merges", merges)

    n_patients = len(set(rid_map.values()))
    summary = {
        "reports": len(records), "duplicate_files": len(inventory) - len(records),
        "patients": n_patients, "patients_with_multiple_reports": sum(1 for rs in by_patient.values() if len(rs) > 1),
        "parse_warnings": dict(Counter(w for r in records for w in r["warnings"])),
        "stop_reasons": dict(Counter(r["parsed"]["stop_reason"] for r in records)),
        "modality": dict(Counter(r["cls"]["modality"] for r in records)),
        "linkage_review_pairs": dict(Counter(v[2] for v in review.values())),
        "linkage_review_undecided_records": len(pending),
        "qa_passed": len(passed), "qa_failed": len(staged_all) - len(passed),
        "qa_issue_counts": dict(issue_counter), "human_sample_rows": n_sample,
        "seconds": round(time.time() - t0, 1),
    }
    (cfg.secure_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    vault.log_access("run", reports=len(records), passed=len(passed))
    print(json.dumps(summary, indent=2))
    print(f"\nNext (all files are in {cfg.secure_dir}):\n"
          f"  1. linkage_review.csv   -> write same/different in 'decision', re-run\n"
          f"  2. qa_issues.csv        -> fix or add report_id,pass|fail to qa_overrides.csv, re-run\n"
          f"  3. qa_human_sample.csv  -> compare with the source file, set reviewer_ok=yes/no\n"
          f"  4. python -m pipeline release")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "release", "dicom"])
    ap.add_argument("--raw", type=Path, default=RAW_DEFAULT, help="raw .doc folder (read only)")
    ap.add_argument("--secure", type=Path, default=RAW_DEFAULT.parent / "Nayon_secure",
                    help="vault folder: mappings, review queues (put on encrypted storage)")
    ap.add_argument("--research", type=Path, default=RAW_DEFAULT.parent / "Nayon_research",
                    help="output folder for the released de-identified dataset")
    ap.add_argument("--key-file", type=Path, default=RAW_DEFAULT.parent / "Nayon_keys" / "master.key")
    ap.add_argument("--antiword", help="path to antiword.exe")
    ap.add_argument("--limit", type=int, help="process only the first N files (testing)")
    ap.add_argument("--allow-pending-linkage", action="store_true",
                    help="do not block records whose linkage is still under review")
    ap.add_argument("--input", type=Path, help="dicom: folder with .dcm files")
    ap.add_argument("--dicom-out", type=Path, help="dicom: output folder (default <research>/images)")
    a = ap.parse_args(argv)
    cfg = Config(raw_dir=a.raw.resolve(), secure_dir=a.secure.resolve(), research_dir=a.research.resolve(),
                 key_file=a.key_file.resolve(), antiword=a.antiword, limit=a.limit,
                 block_pending_linkage=not a.allow_pending_linkage)
    for p in (cfg.secure_dir, cfg.research_dir):
        if p == cfg.raw_dir or cfg.raw_dir in p.parents:
            sys.exit(f"{p} is inside the raw folder; choose a separate location")
    cfg.secure_dir.mkdir(parents=True, exist_ok=True)

    if a.command == "run":
        sys.exit(cmd_run(cfg))
    if a.command == "release":
        from .release import release
        vault = Vault(cfg.secure_dir, cfg.key_file)
        code = release(cfg.secure_dir, cfg.research_dir, cfg.k_min)
        vault.log_access("release", ok=code == 0)
        sys.exit(code)
    if a.command == "dicom":
        if not a.input:
            sys.exit("dicom needs --input <folder>")
        from .dicom_deid import deidentify_folder
        vault = Vault(cfg.secure_dir, cfg.key_file)
        code = deidentify_folder(a.input, a.dicom_out or cfg.research_dir / "images", cfg.secure_dir, vault)
        vault.log_access("dicom", ok=code == 0)
        sys.exit(code)
