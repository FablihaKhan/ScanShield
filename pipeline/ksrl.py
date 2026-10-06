"""Step 3: KSRL-style Patient Identification Key (PIK), after Fig. 2 of the NCDW paper.

Paper:  NAME -> significant portion -> masked (vowels dropped) -> keyed -> hash
        GENDER code + DoB birth-year 5-year range            -> hash
        ADDRESS -> SOUNDEX                                  -> hash
        PIK = concatenation of the three hashes

Adaptations for this data set (documented so they can be cited in the thesis):
  * Every hash is HMAC-SHA256 under a secret key (vault.py), not a plain hash,
    so the PIK cannot be reproduced by guessing names/birth years.
  * Reports carry Age, not DoB: birth year is estimated as report_year - age.
    That estimate is +/-1 year noisy, so linkage.py compares the estimates with
    a tolerance instead of requiring the exact 5-year bin to match.
  * Reports carry no address: the address component is the keyed hash of 'NA'.
  * Name tokens are sorted before masking so 'Rahima Begum' == 'Begum Rahima'.
  * 'h' and 'w' are dropped with vowels, which merges common spelling variants
    (Hossain/Hosen/Hussain -> hsn, Rahman/Rahaman -> rmn, Chowdhury/Choudhury -> cdr).
"""
import re

# honorifics / religious prefixes that the paper strips as "non-significant"
TITLES = {
    "mr", "mrs", "ms", "miss", "md", "mohammad", "mohammed", "muhammad", "mohamed", "mohd",
    "mohammod", "muhammed", "mohamad", "mst", "most", "mosammat", "mossammat", "mosamat",
    "musammat", "mosammet", "dr", "prof", "late", "alhaj", "haji", "hajee", "hazi", "janab",
    "sri", "shri", "srimoti", "smt",
}


def significant_tokens(name: str | None) -> list[str]:
    toks = re.findall(r"[a-z]+", (name or "").lower())
    return [t for t in toks if t not in TITLES and len(t) > 1]


def mask_token(tok: str) -> str:
    tok = re.sub(r"(.)\1+", r"\1", tok)
    return tok[0] + re.sub(r"[aeiouyhw]", "", tok[1:])


def masked_name(name: str | None) -> str:
    return " ".join(sorted(mask_token(t) for t in significant_tokens(name)))


def birth_year_range(birth_year: int | None, width: int = 5) -> str:
    if birth_year is None:
        return "NA"
    start = birth_year - birth_year % width
    return f"{start}-{start + width - 1}"


def soundex(text: str | None) -> str:
    s = re.sub(r"[^a-z]", "", (text or "").lower())
    if not s:
        return "NA"
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
             **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
    out, prev = s[0].upper(), codes.get(s[0], "")
    for ch in s[1:]:
        code = codes.get(ch, "")
        if code and code != prev:
            out += code
        if ch not in "hw":
            prev = code
    return (out + "000")[:4]


def generate_pik(vault, name: str | None, sex: str, birth_year: int | None,
                 address: str | None = None) -> dict:
    masked = masked_name(name)
    name_part = vault.keyed_hash("ksrl:name", masked)
    demo_part = vault.keyed_hash("ksrl:demo", f"{sex}{birth_year_range(birth_year)}")
    addr_part = vault.keyed_hash("ksrl:addr", soundex(address) if address else "NA")
    return {"masked_name": masked, "name_part": name_part, "demo_part": demo_part,
            "addr_part": addr_part, "pik": (name_part + demo_part + addr_part).upper(),
            "n_tokens": len(masked.split())}
