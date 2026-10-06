"""Privacy-preserving radiology report pipeline (NCDW-style, KSRL linkage).

Steps implemented (see README.md):
  1  raw data isolation + encrypted vault + access log      -> vault.py, run_pipeline.py
  2  inventory without names                                 -> run_pipeline.py
  3  KSRL PIK + human-reviewed linkage + random research IDs -> ksrl.py, linkage.py
  4  report de-identification (text, filename, Word metadata) -> extract.py, parse_report.py, deidentify.py
  5  DICOM de-identification (for when images arrive)         -> dicom_deid.py
  6  per-patient secret date shift                            -> deidentify.py
  7  research_patient_id -> encounter -> report -> study      -> run_pipeline.py
  8  privacy QA gate (automatic + human sample sign-off)      -> qa.py
  9/10 release, cohort summary, small-cell check              -> release.py
"""
