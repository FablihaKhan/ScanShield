"""Source-specific wrapper, extraction part (paper section 3.1).

Reads legacy Word 97-2003 (.doc, OLE2) reports with antiword and pulls the
OLE SummaryInformation metadata (author, last saved by, ...). The metadata is
never copied downstream; it is only used as extra deny-terms for the QA scan.

Nothing here prints file names or text: ~115 source file names contain
patient names, so errors are reported by index only.
"""
import hashlib
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import olefile

_ANTIWORD_CANDIDATES = [
    r"C:\Program Files\Git\mingw64\bin\antiword.exe",
    r"D:\Git\mingw64\bin\antiword.exe",
]
_FOLDER_DATE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_META_FIELDS = ("author", "last_saved_by", "title", "subject", "keywords", "comments",
                "company", "manager")


class ExtractionError(Exception):
    pass


def find_antiword(explicit: str | None = None) -> str:
    for cand in [explicit, shutil.which("antiword"), *_ANTIWORD_CANDIDATES]:
        if cand and Path(cand).exists():
            return str(cand)
    raise FileNotFoundError("antiword not found; pass --antiword <path to antiword.exe>")


def list_source_files(raw_dir: Path) -> tuple[list[Path], int]:
    """All .doc reports, skipping Word lock files (~$...). Returns (files, n_lock_files)."""
    files, locks = [], 0
    for p in sorted(Path(raw_dir).rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".doc":
            continue
        if p.name.startswith("~$"):
            locks += 1
            continue
        files.append(p)
    return files, locks


def folder_date(folder_name: str) -> date | None:
    m = _FOLDER_DATE_RE.match(folder_name)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def filename_tokens(path: Path) -> list[str]:
    """Alphabetic fragments of the file name (often a patient name)."""
    return [t.lower() for t in re.findall(r"[A-Za-z]{2,}", Path(path).stem)]


def _doc_to_text(path: Path, antiword: str) -> str:
    try:
        r = subprocess.run([antiword, "-m", "UTF-8.txt", "-w", "0", str(path)],
                           capture_output=True, timeout=120)
        if r.returncode != 0:  # mapping file not found outside the Git shell, etc.
            r = subprocess.run([antiword, "-w", "0", str(path)], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise ExtractionError("antiword timeout") from e
    if r.returncode != 0:
        raise ExtractionError(f"antiword exit code {r.returncode}")
    return r.stdout.decode("utf-8", errors="replace")


def _ole_metadata(path: Path) -> dict:
    try:
        with olefile.OleFileIO(str(path)) as ole:
            meta = ole.get_metadata()
    except Exception:
        return {}
    out = {}
    for field in _META_FIELDS:
        v = getattr(meta, field, None)
        if isinstance(v, bytes):
            v = v.decode("cp1252", errors="replace")
        if v:
            out[field] = str(v).strip("\x00 ").strip()
    return out


def _extract_one(args) -> dict:
    idx, path, raw_dir, antiword = args
    data = path.read_bytes()
    rec = {"index": idx, "path": str(path), "rel": str(path.relative_to(raw_dir)),
           "folder": path.parent.name, "sha256": hashlib.sha256(data).hexdigest(),
           "size": len(data), "is_ole": data[:8] == bytes.fromhex("d0cf11e0a1b11ae1"),
           "text": "", "meta": {}, "error": None}
    try:
        rec["text"] = _doc_to_text(path, antiword)
    except ExtractionError as e:
        rec["error"] = str(e)
    rec["meta"] = _ole_metadata(path)
    return rec


def extract_all(paths: list[Path], raw_dir: Path, antiword: str, workers: int = 8) -> list[dict]:
    jobs = [(i, p, Path(raw_dir), antiword) for i, p in enumerate(paths)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_extract_one, jobs))
