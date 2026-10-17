"""Pick a diverse test subset of the Rakathon dataset and write it as ndjson
next to the dataset (never into the repo).

Diversity comes from two axes, because k-anonymity leaves each patient with at
most two ICD-10 codes:
- diagnosis: every one of the 41 codes, plus every patient with two diagnoses;
- data richness: rich, middle and sparse patients, so matching sees both full
  records and records that should end UNKNOWN.

Every diagnosis gets the same share (all of its patients if it has fewer).
Deterministic: the same dataset always gives the same subset.
Prints aggregate counts only.
"""

import csv
import json
import pathlib
from collections import Counter

DATASET = pathlib.Path("/Users/roman/Downloads/rakathon-dataset-20261016-081440-33e1d8")
OUT = DATASET / "fhir" / "subset-diverse.ndjson"

PER_CODE = 8


def richness(row: dict) -> int:
    return sum(int(row[c] or 0) for c in ("zprav", "vysetreni", "lab_vysledku", "leku"))


def stratified(rows: list[dict], n: int) -> list[dict]:
    """Evenly spaced picks over the richness ranking: rich, middle and sparse."""
    ranked = sorted(rows, key=lambda r: (-richness(r), r["pseudonym"]))
    if len(ranked) <= n:
        return ranked
    step = len(ranked) / n
    return [ranked[int(i * step)] for i in range(n)]


def main() -> None:
    with open(DATASET / "kohorta.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        r["codes"] = r["diagnozy"].split()

    chosen: dict[str, str] = {}  # pseudonym -> reason

    for r in rows:
        if len(r["codes"]) >= 2:
            chosen.setdefault(r["pseudonym"], "two diagnoses")

    for code in sorted({c for r in rows for c in r["codes"]}):
        for r in stratified([r for r in rows if code in r["codes"]], PER_CODE):
            chosen.setdefault(r["pseudonym"], "diagnosis coverage")

    for r in stratified([r for r in rows if not r["codes"]], PER_CODE):
        chosen.setdefault(r["pseudonym"], "no coded diagnosis")

    written = 0
    with open(DATASET / "fhir" / "bundles.ndjson", "rb") as src, open(OUT, "wb") as out:
        for line in src:
            bundle = json.loads(line)
            pid = next(e["resource"]["id"] for e in bundle["entry"]
                       if e["resource"]["resourceType"] == "Patient")
            if pid in chosen:
                out.write(line if line.endswith(b"\n") else line + b"\n")
                written += 1

    picked = [r for r in rows if r["pseudonym"] in chosen]
    print(f"selected {len(chosen)}, written {written} -> {OUT.name}")
    print("by reason:", dict(Counter(chosen.values())))
    print("diagnosis codes covered:", len({c for r in picked for c in r["codes"]}),
          "of", len({c for r in rows for c in r["codes"]}))
    print("sex:", dict(Counter(r["pohlavi"] for r in picked)))
    for col in ("zprav", "vysetreni", "lab_vysledku", "leku", "vykonu"):
        print(f"{col}: total {sum(int(r[col] or 0) for r in picked)}, "
              f"patients with none {sum(not int(r[col] or 0) for r in picked)}")


if __name__ == "__main__":
    main()
