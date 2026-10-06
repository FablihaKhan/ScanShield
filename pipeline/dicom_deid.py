"""Step 5: DICOM de-identification (the current folder has no images; this is
ready for when CT studies are exported).

* every Person Name (PN) element, anywhere including sequences, is removed
* listed identifying tags and all private tags are removed
* PatientName/PatientID become the research_patient_id
* study/series/instance UIDs are remapped with a keyed hash (consistent across files)
* dates are shifted with the same per-patient secret shift as the reports
* files that may carry burned-in text (secondary capture, dose reports, US,
  BurnedInAnnotation=YES, ...) are flagged for manual pixel review

A study is linked to a patient by its PatientID / AccessionNumber matching the
report ID NO (compared via keyed hash), then to the report with the nearest
report date (<= 3 days). Unlinked studies are listed for human review and not output.
"""
import csv
from datetime import datetime, timedelta
from pathlib import Path

import pydicom

REMOVE_TAGS = [
    "PatientBirthDate", "PatientBirthTime", "PatientAddress", "PatientTelephoneNumbers",
    "OtherPatientIDs", "OtherPatientIDsSequence", "OtherPatientNames", "PatientMotherBirthName",
    "MedicalRecordLocator", "IssuerOfPatientID", "AccessionNumber", "StudyID",
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName", "StationName",
    "DeviceSerialNumber", "RequestAttributesSequence", "AdmittingDiagnosesDescription",
    "AdditionalPatientHistory", "PatientComments", "ImageComments", "StudyComments",
    "CountryOfResidence", "RegionOfResidence", "Occupation", "PatientReligiousPreference",
    "PatientInsurancePlanCodeSequence", "MilitaryRank", "BranchOfService",
]
DATE_TAGS = ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"]
UID_TAGS = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "FrameOfReferenceUID"]
PIXEL_RISK_MODALITIES = {"US", "SC", "OT", "XC", "SR", "DOC"}


def _new_uid(vault, old: str) -> str:
    return "2.25." + str(int(vault.keyed_hash("dicom-uid", old, 30), 16))


def _drop_person_names(ds, elem):
    if elem.VR == "PN":
        del ds[elem.tag]


def deidentify_file(src: Path, dst_root: Path, research_id: str, shift_days: int, vault) -> dict:
    ds = pydicom.dcmread(str(src))
    ds.remove_private_tags()
    ds.walk(_drop_person_names)
    for kw in REMOVE_TAGS:
        if kw in ds:
            delattr(ds, kw)
    ds.PatientName = research_id
    ds.PatientID = research_id
    age = str(ds.get("PatientAge", ""))
    if len(age) == 4 and age.endswith("Y") and age[:3].isdigit() and int(age[:3]) >= 90:
        ds.PatientAge = "090Y"
    for kw in DATE_TAGS:
        v = str(ds.get(kw, "") or "")
        if len(v) == 8:
            setattr(ds, kw, (datetime.strptime(v, "%Y%m%d") + timedelta(days=shift_days)).strftime("%Y%m%d"))
    for kw in UID_TAGS:
        if kw in ds:
            setattr(ds, kw, _new_uid(vault, str(getattr(ds, kw))))
    if getattr(ds, "file_meta", None) is not None and "SOPInstanceUID" in ds:
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = "Nayon pipeline: PN/private tags removed, dates shifted, UIDs remapped"

    image_type = " ".join(map(str, ds.get("ImageType", [])))
    pixel_review = (str(ds.get("BurnedInAnnotation", "")).upper() == "YES"
                    or str(ds.get("Modality", "")) in PIXEL_RISK_MODALITIES
                    or "SECONDARY" in image_type.upper() or "SCREEN" in image_type.upper())
    out = dst_root / research_id / str(ds.get("StudyInstanceUID", "study")) / f"{ds.get('SOPInstanceUID', src.stem)}.dcm"
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(out))
    return {"study_id": str(ds.get("StudyInstanceUID", "")), "output": str(out), "needs_pixel_review": pixel_review}


def deidentify_folder(input_dir: Path, output_dir: Path, secure_dir: Path, vault) -> int:
    identity = vault.load("identity_map", {})
    shifts = vault.load("date_shifts", {})
    links = vault.load("image_links", {})
    id_index = {}
    for uid, rec in identity.items():
        if rec.get("id_no"):
            id_index.setdefault(vault.keyed_hash("idno", rec["id_no"]), []).append(uid)

    manifest, unlinked = [], []
    for src in sorted(Path(input_dir).rglob("*")):
        if not src.is_file():
            continue
        try:
            hdr = pydicom.dcmread(str(src), stop_before_pixels=True)
        except Exception:
            continue
        cand = []
        for key in (hdr.get("PatientID"), hdr.get("AccessionNumber")):
            if key:
                cand += id_index.get(vault.keyed_hash("idno", "".join(str(key).split())), [])
        study_date = str(hdr.get("StudyDate", "") or "")
        best = None
        if cand and len(study_date) == 8:
            sd = datetime.strptime(study_date, "%Y%m%d").date()
            dist = [(abs((datetime.fromisoformat(identity[u]["report_date"]).date() - sd).days), u)
                    for u in cand if identity[u].get("report_date")]
            dist = [d for d in dist if d[0] <= 3]
            best = min(dist)[1] if dist else None
        if not best:
            unlinked.append({"file": str(src), "reason": "no report with matching ID NO within 3 days"})
            continue
        rid = identity[best]["research_patient_id"]
        res = deidentify_file(src, Path(output_dir), rid, shifts[rid], vault)
        links[best] = res["study_id"]
        manifest.append({"file": str(src), "report_uid": best, **res})

    vault.save("image_links", links)
    for name, rows in (("dicom_manifest.csv", manifest), ("dicom_unlinked.csv", unlinked)):
        if rows:
            with open(secure_dir / name, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    print(f"DICOM: {len(manifest)} files de-identified, {sum(m['needs_pixel_review'] for m in manifest)} need "
          f"pixel review, {len(unlinked)} unlinked (see {secure_dir}). Re-run `run` to attach study_id.")
    return 0
