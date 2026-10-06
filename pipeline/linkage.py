"""Step 3: record linkage on KSRL keys with a human review queue.

Auto-merge only when the evidence is strong; anything doubtful becomes a row
in linkage_review.csv (inside the secure vault folder) and is NOT merged until
a person writes 'same' or 'different' in its `decision` column.

  auto  : same keyed name + same sex + same keyed ID NO
  auto  : same keyed name (>= 2 significant tokens) + same sex + birth-year estimates within 1
  review: same keyed name + same sex, but single-token name / missing age / birth years 2 apart
  review: same ID NO but different name or sex
  review: different keyed name, same sex, birth years within 2, Jaro-Winkler(name) >= 0.92
"""
import csv
import secrets
from collections import defaultdict
from itertools import combinations
from pathlib import Path

REVIEW_COLUMNS = ["pair_key", "reason", "decision", "record_a", "record_b",
                  "name_a", "name_b", "sex_a", "sex_b", "age_a", "age_b",
                  "date_a", "date_b", "id_no_a", "id_no_b", "file_a", "file_b"]


class _DSU:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    rng = max(la, lb) // 2 - 1
    ma, mb = [False] * la, [False] * lb
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - rng), min(lb, i + rng + 1)):
            if not mb[j] and b[j] == ch:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    k = trans = 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            trans += a[i] != b[k]
            k += 1
    jaro = (matches / la + matches / lb + (matches - trans / 2) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _pair_key(a, b):
    return "|".join(sorted((a, b)))


def load_decisions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["pair_key"]: (r.get("decision") or "").strip().lower() for r in csv.DictReader(f)}


def link_records(recs: list[dict], decisions: dict[str, str]):
    """recs: dicts with uid, name_part, n_tokens, name_norm, sex, birth_year, id_hash.
    Returns (uid -> cluster root, review rows {pair_key: (a, b, reason)}, pending uids)."""
    by_uid = {r["uid"]: r for r in recs}
    dsu = _DSU(by_uid)
    review: dict[str, tuple] = {}

    def dby(a, b):
        if a["birth_year"] is None or b["birth_year"] is None:
            return None
        return abs(a["birth_year"] - b["birth_year"])

    groups = defaultdict(list)
    for r in recs:
        if r["n_tokens"]:
            groups[(r["name_part"], r["sex"])].append(r)
    for grp in groups.values():
        for a, b in combinations(grp, 2):
            d = dby(a, b)
            if a["id_hash"] and a["id_hash"] == b["id_hash"]:
                dsu.union(a["uid"], b["uid"])
            elif d is None:
                review[_pair_key(a["uid"], b["uid"])] = (a["uid"], b["uid"], "same_name_missing_age")
            elif d <= 1 and a["n_tokens"] >= 2:
                dsu.union(a["uid"], b["uid"])
            elif d <= 1:
                review[_pair_key(a["uid"], b["uid"])] = (a["uid"], b["uid"], "single_token_name")
            elif d == 2:
                review[_pair_key(a["uid"], b["uid"])] = (a["uid"], b["uid"], "same_name_birth_year_differs")
            # d >= 3: birth-year estimates are +/-1 accurate, so treat as different people

    by_id = defaultdict(list)
    for r in recs:
        if r["id_hash"]:
            by_id[r["id_hash"]].append(r)
    for grp in by_id.values():
        for a, b in combinations(grp, 2):
            if a["name_part"] != b["name_part"] or a["sex"] != b["sex"]:
                review[_pair_key(a["uid"], b["uid"])] = (a["uid"], b["uid"], "same_id_no_different_identity")

    blocks = defaultdict(list)
    for r in recs:
        if r["name_norm"]:
            blocks[(r["sex"], r["name_norm"][0])].append(r)
    for grp in blocks.values():
        for a, b in combinations(grp, 2):
            if a["name_part"] == b["name_part"]:
                continue
            d = dby(a, b)
            if d is not None and d > 2:
                continue
            key = _pair_key(a["uid"], b["uid"])
            if key in review or dsu.find(a["uid"]) == dsu.find(b["uid"]):
                continue
            if jaro_winkler(a["name_norm"], b["name_norm"]) >= 0.92:
                review[key] = (a["uid"], b["uid"], "similar_name")

    for key, (a, b, _) in review.items():
        if decisions.get(key) == "same":
            dsu.union(a, b)
    # undecided pairs that are not already joined through other evidence
    pending = set()
    for key, (a, b, _) in review.items():
        if decisions.get(key) not in ("same", "different") and dsu.find(a) != dsu.find(b):
            pending.update((a, b))
    clusters = {u: dsu.find(u) for u in by_uid}
    return clusters, review, pending


def write_review_file(path: Path, review: dict, decisions: dict, identity: dict) -> None:
    rows = []
    for key, (a, b, reason) in sorted(review.items(), key=lambda kv: kv[1][2]):
        ia, ib = identity[a], identity[b]
        rows.append({"pair_key": key, "reason": reason, "decision": decisions.get(key, ""),
                     "record_a": a, "record_b": b,
                     "name_a": ia["patient_name"], "name_b": ib["patient_name"],
                     "sex_a": ia["sex"], "sex_b": ib["sex"], "age_a": ia["age_raw"], "age_b": ib["age_raw"],
                     "date_a": ia["report_date"], "date_b": ib["report_date"],
                     "id_no_a": ia["id_no"], "id_no_b": ib["id_no"],
                     "file_a": ia["source"], "file_b": ib["source"]})
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def assign_research_ids(clusters: dict[str, str], existing: dict[str, str]):
    """Stable random research IDs: a cluster keeps the ID one of its members already had.
    Returns (uid -> research_patient_id, list of retired IDs merged into another)."""
    members = defaultdict(list)
    for uid, root in clusters.items():
        members[root].append(uid)
    used = set(existing.values())
    out, retired = {}, []
    for uids in members.values():
        prior = sorted({existing[u] for u in uids if u in existing})
        if prior:
            rid = prior[0]
            retired += [(old, rid) for old in prior[1:]]
        else:
            rid = _new_id("RP", used)
        for u in uids:
            out[u] = rid
    return out, retired


def _new_id(prefix: str, used: set) -> str:
    while True:
        rid = f"{prefix}-{secrets.token_hex(5).upper()}"
        if rid not in used:
            used.add(rid)
            return rid
