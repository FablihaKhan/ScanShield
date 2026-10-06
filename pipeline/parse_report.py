"""Parse antiword output of one CT report into header fields + clinical sections.

Observed layout (antiword renders Word tables with '|' between cells):

    CT SCAN REPORT
    |ID NO  :        |<id>          |Date:  <d Month yyyy>
    |Patient Name  : |<name>        |Age: <n> Yrs    |Sex: M
    |Examined  :     |CT scan of ...
    |Multiple axial ... were performed.         <- technique (no heading)
    |Findings:
    |   |...
    |Impression:     |...
    <signature blocks: DR./PROF./MBBS/Department of Radiology ...>
    <template appendix: more Findings:/Impression: blocks, other signatures>

Only the first report is kept; parsing stops at the first signature line,
new report title or non-advice heading after the Impression section.
"""
import difflib
import re
from datetime import date

_HEADER_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(id\s*no\.?|reg(?:istration)?\.?\s*no\.?|patient'?s?\s*name|name"
    r"|m(?:r|rs|iss|s)\.?\s*/\s*m(?:r|rs|iss|s)\.?(?:\s*/\s*[a-z]+\.?)?"
    r"|date|age|sex|gender|examined|examination)\s*:",
    re.I)
_UNLABELLED_EXAM_RE = re.compile(r"^(?:hr)?ct\b.*\bof\b|^(?:mri|cta|ncct|cect)\b", re.I)


def _header_key(label: str) -> str:
    k = re.sub(r"[^a-z]", "", label.lower())
    if k.startswith(("idno", "reg")):
        return "id_no"
    if k.startswith(("patient", "name")) or k.startswith("m"):
        return "patient_name"
    if k.startswith("date"):
        return "date"
    if k.startswith("age"):
        return "age"
    if k.startswith(("sex", "gender")):
        return "sex"
    return "examined"


def _header_pairs(cells: list[str]) -> list[tuple[str, str]]:
    """All 'Label: value' pairs on one line, even several inside one cell."""
    line = " \x1f ".join(cells)
    ms = list(_HEADER_LABEL_RE.finditer(line))
    pairs = []
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(line)
        val = " ".join(p.strip() for p in line[m.end():end].split("\x1f") if p.strip())
        pairs.append((_header_key(m.group(1)), val.strip()))
    return pairs

_SECTION_WORDS = (r"clinical\s+(?:information|info|history|details)|history|technique|findings?"
                  r"|impressions?|conclusion|opinion|advice|adv|suggestions?|n\.?\s?b\.?")
_SECTION_RE = re.compile(rf"^\s*(?:[o•\-*]\s+)?({_SECTION_WORDS})\s*[:\-–]\s*(.*)$", re.I)
_BARE_SECTION_RE = re.compile(rf"^\s*(?:[o•\-*]\s+)?({_SECTION_WORDS})\s*$", re.I)

SIGNATURE_RE = re.compile(
    r"^\s*(?:dr|prof|professor)\b\.?|\bMBBS\b|\bFCPS\b|\bM\.?\s?Phil\b|\bDMRD\b|\bMRCR\b|\bRDMS\b"
    r"|department\s+of\s+radiology|medical\s+university|university\s*,|resident\s*\(|phase\s*-?\s*[ab]\b"
    r"|medical\s+officer|consultant\s+radiologist|assistant\s+professor|associate\s+professor"
    r"|\btrainee\b|clinical\s+fellow",
    re.I)
_TITLE_RE = re.compile(r"\bREPORT\b")

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _canonical_section(word: str) -> str:
    w = word.lower()
    if w.startswith(("clinical", "history")):
        return "clinical_information"
    if w.startswith("technique"):
        return "technique"
    if w.startswith("finding"):
        return "findings"
    if w.startswith(("impression", "conclusion", "opinion")):
        return "impression"
    return "advice"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"[|\t\x07�]", line) if c.strip()]


def _section_match(text: str):
    m = _SECTION_RE.match(text)
    if m:
        return _canonical_section(m.group(1)), m.group(2).strip()
    m = _BARE_SECTION_RE.match(text)
    if m:
        return _canonical_section(m.group(1)), ""
    return None


def parse_report(text: str) -> dict:
    lines = [c for c in (_cells(l) for l in text.splitlines()) if c]
    header = {k: None for k in ("id_no", "patient_name", "date", "age", "sex", "examined")}
    sections = {k: [] for k in ("technique", "clinical_information", "findings", "impression", "advice")}
    out = {"title": None, "header": header, "warnings": [], "stop_reason": "end_of_document"}

    # --- header -----------------------------------------------------------
    i, waiting = 0, None  # waiting: label seen with its value on the next line
    while i < len(lines) and i < 40:
        cells = lines[i]
        joined = " ".join(cells)
        if _section_match(joined):
            break
        if out["title"] is None and _TITLE_RE.search(joined) and joined.upper() == joined:
            out["title"] = joined.strip()
            i += 1
            continue
        pairs = _header_pairs(cells)
        if not pairs and waiting:
            header[waiting], waiting = joined.strip(), None
        elif not pairs and header["examined"] is None and _UNLABELLED_EXAM_RE.search(joined):
            header["examined"] = joined.strip()
        for key, val in pairs:
            if header[key] is None:
                header[key] = val or None
            waiting = key if not val and header[key] is None else None
        i += 1
        if header["examined"] is not None and not waiting:
            break
    if header["id_no"] is None and header["patient_name"] is None:
        out["warnings"].append("no_header")

    # --- body -------------------------------------------------------------
    current, seen_impression = "technique", False
    for cells in lines[i:]:
        t = " ".join(cells).strip()
        if SIGNATURE_RE.search(t):
            if seen_impression:
                out["stop_reason"] = "signature"
                break
            continue  # never keep clinician lines
        if seen_impression and _TITLE_RE.search(t) and t.upper() == t:
            out["stop_reason"] = "new_report"
            break
        sm = _section_match(t)
        if sm:
            sec, rest = sm
            if seen_impression and sec != "advice":
                out["stop_reason"] = "template_appendix"
                break
            current = sec
            seen_impression |= sec == "impression"
            if rest:
                sections[sec].append(rest)
            continue
        if current == "technique" and not seen_impression and _header_pairs(cells):
            continue  # stray header line (e.g. repeated Name/Date) - never keep
        sections[current].append(t)

    out["sections"] = {k: "\n".join(v).strip() for k, v in sections.items()}
    if not out["sections"]["impression"]:
        out["warnings"].append("no_impression")
    if not out["sections"]["findings"]:
        out["warnings"].append("no_findings")
    return out


# --- field normalisers ------------------------------------------------------

def _month(word: str) -> int | None:
    w = word.lower()
    for k, name in enumerate(_MONTHS, 1):
        if name.startswith(w[:3]) and len(w) >= 3:
            return k
    close = difflib.get_close_matches(w, _MONTHS, n=1, cutoff=0.7)
    return _MONTHS.index(close[0]) + 1 if close else None


def _year(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 100 else y


def parse_date(s: str | None) -> date | None:
    """Accepts '12 October 2024', 'October 12, 2024', '12/10/2024' (dd/mm), '2024-10-12'."""
    if not s:
        return None
    candidates = []
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[\s,.\-/]*([A-Za-z]{3,})[\s,.\-/]*(\d{4}|\d{2})\b", s)
    if m and _month(m.group(2)):
        candidates.append((_year(m.group(3)), _month(m.group(2)), int(m.group(1))))
    m = re.search(r"([A-Za-z]{3,})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and _month(m.group(1)):
        candidates.append((int(m.group(3)), _month(m.group(1)), int(m.group(2))))
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        candidates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = re.search(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{4}|\d{2})\b", s)
    if m:
        candidates.append((_year(m.group(3)), int(m.group(2)), int(m.group(1))))
    for y, mo, d in candidates:
        try:
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def parse_age_years(s: str | None) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", s or "")
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("m") and not unit.startswith("mi"):
        v /= 12
    elif unit.startswith("d"):
        v /= 365.25
    elif unit.startswith("w"):
        v /= 52.18
    return v if 0 <= v <= 120 else None


def parse_sex(s: str | None) -> str:
    s = (s or "").strip().lower()
    return "M" if s.startswith("m") else "F" if s.startswith("f") else "U"


_REGIONS = {
    "brain/head": r"brain|head|cranial|skull|sella|pituitary",
    "chest": r"chest|thora|lung|pulmonary|mediastin",
    "abdomen": r"abdom|kub|liver|hepat|pancrea|renal|kidney|urogra|entero",
    "pelvis": r"pelvi",
    "neck": r"neck|thyroid|laryn|pharyn|parotid",
    "paranasal sinus": r"\bpns\b|sinus|nasal",
    "temporal bone/ear": r"temporal\s+bone|mastoid|\bear\b|\bhrct\s+of\s+(?:rt|lt|right|left|both)",
    "orbit": r"orbit",
    "spine": r"spin|vertebr|cervical|lumbar|dorsal|sacr",
    "extremity": r"knee|shoulder|hip|ankle|wrist|elbow|limb|extremit|femur|tibia|hand|foot",
    "vascular": r"angiogra|\bcta\b|aort|venogra",
}


def classify_exam(examined: str, technique: str = "", title: str = "") -> dict:
    e = (examined or "").lower()
    both = f"{e} {(technique or '').lower()}"
    if "hrct" in e:
        modality = "HRCT"
    elif re.search(r"\bcta\b|angiogra", e):
        modality = "CT angiography"
    elif re.search(r"\bmri?\b|magnetic", e):
        modality = "MRI"
    elif re.search(r"\bct\b|\bcect\b|\bncct\b|computed", e):
        modality = "CT"
    elif re.search(r"x[\s-]?ray", f"{e} {(title or '').lower()}"):
        modality = "X-ray"
    elif re.search(r"\bct\b", (title or "").lower()):
        modality = "CT"  # exam text lacks modality, report title says CT
    else:
        modality = "other"
    if re.search(r"with\s+contrast|contrast[\s-]+enhanced|\bcect\b|\biv\s+contrast|after\s+iv|and\s+contrast", both):
        contrast = "with contrast"
    elif re.search(r"non[\s-]*contrast|plain|without\s+contrast|\bncct\b", both):
        contrast = "without contrast"
    else:
        contrast = "unspecified"
    regions = [r for r, rx in _REGIONS.items() if re.search(rx, e)]
    return {"modality": modality, "contrast": contrast, "body_region": ";".join(regions) or "unspecified"}
