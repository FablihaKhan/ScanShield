# ScanShield

Privacy protection for hospital radiology reports.

This is the code I wrote for my thesis work on privacy-preserving clinical data. It takes a collection of hospital radiology reports (CT, HRCT and a few X-ray reports saved as old Word `.doc` files) and turns them into a dataset researchers can use without seeing who the patients are.

The design follows the National Clinical Data Warehouse (NCDW) architecture from Mia et al. (Smart Health, 2022), and the patient linking uses their Key-based Secured Record Linkage (KSRL) method. That paper only covered lab records and listed radiology as future work, so this project tries to fill that gap.

No patient data is included in this repository. Only the code is here.

## What it does

In short, the pipeline:

1. Reads each `.doc` report and pulls out the header (ID, name, age, sex, date, exam) and the clinical sections (clinical information, technique, findings, impression, advice). The doctor's signature and the template text that follows each report are cut off.
2. Builds a Patient Identification Key (PIK) for each report from the masked name, sex and an estimated birth-year range, as in the KSRL paper. Every hash is an HMAC with a secret key, so nobody can rebuild a key by guessing common names and birth years.
3. Links reports that belong to the same person. Only clear matches are merged automatically. Doubtful pairs go into a review file and wait until a person marks them `same` or `different`.
4. Gives each patient a random research ID, and gives each encounter and report its own random ID. The mapping back to real identities is stored encrypted.
5. Removes identifiers from the report text: patient names, ID numbers, doctor names and degrees, phone numbers, e-mails and long numbers. Dates are moved by a secret per-patient offset, so the time between visits is kept. Ages of 90 and over become `90+`.
6. Runs a privacy check on every record. A record is held back if anything still looks like a name, ID or phone number, or if parsing failed. A person also has to approve a random 5% sample.
7. Writes the final research files only after that approval. It also lists small subgroups (fewer than 5 patients) that should not be published as they are.

There is also a DICOM module for when the CT images become available. It strips patient tags, remaps UIDs and flags images that might have text burned into the pixels.

## Project layout

```
pipeline/
  run_pipeline.py   command line entry point, runs the main steps in order
  extract.py        reads .doc files (antiword) and Word metadata (olefile)
  parse_report.py   splits a report into header fields and sections
  ksrl.py           name masking, SOUNDEX and PIK generation
  linkage.py        matching rules, Jaro-Winkler, review queue, research IDs
  deidentify.py     text scrubbing, date shifting, age capping
  qa.py             privacy checks and the human review sample
  release.py        builds the final dataset and the small-cell report
  dicom_deid.py     DICOM image de-identification
  vault.py          secret key, keyed hashing, encrypted storage, access log
```

## Running it

You need Python 3.10 or newer and `antiword`, which comes with Git for Windows under `mingw64\bin`.

```
pip install -r pipeline/requirements.txt

python -m pipeline run --raw <reports folder> --secure <vault folder> --research <output folder> --key-file <key path>
```

After the first run, open the vault folder and:

- fill in `linkage_review.csv` with `same` or `different` for each doubtful pair
- check `qa_issues.csv` and fix or override the blocked records
- compare the rows in `qa_human_sample.csv` against the originals and mark them `yes` or `no`

Then run again and release:

```
python -m pipeline run     ...same options...
python -m pipeline release ...same options...
```

If images arrive later:

```
python -m pipeline dicom --input <dicom folder> ...same options...
```

Keep the raw reports, the vault folder and the key file off any shared drive, cloud sync or git repository. The key should live on separate encrypted storage, because the encrypted mappings cannot be read without it.

## Changes from the original KSRL method

- The paper hashes names and birth years with a plain hash. Plain hashes can be reversed by guessing likely names, so this code uses HMAC-SHA256 with a secret key instead.
- The reports give age but not date of birth. Birth year is estimated from age and report date, and two estimates count as a match if they are within one year of each other.
- The reports have no address, so the address part of the key is fixed as `NA`.
- Name parts are sorted, and `h` and `w` are dropped along with the vowels. This lets common spelling variants match, such as Hossain and Hussain, or Chowdhury and Choudhury.
- The PIK never leaves the encrypted vault. The released data only carries random IDs.

## Known limitations

- The rules are written for English text. Bangla names and numbers are not handled yet.
- The DICOM part has not been tested on real images yet.
- Linkage accuracy has not been measured, because there is no ground truth yet. The reviewers' decisions can be used as a labelled set later.
- Rare identifiers in free text, such as a relative's name or a place, may not be caught by the rules. The human sample check is there to catch these, but it cannot remove the risk completely.

## Reference

Mia, M.R., Hoque, A.S.M.L., Khan, S.I., Ahamed, S.I. (2022). A privacy-preserving National Clinical Data Warehouse: Architecture and analysis. Smart Health 23, 100238.

Khan, S.I., Hoque, A.S.L. (2019). Secured technique for healthcare record linkage. Proceedings of the 6th International Conference on Networking, Systems and Security, 30-36.

## Author

Fabliha Afia
