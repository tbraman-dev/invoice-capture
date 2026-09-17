"""Score invoices_out/review.csv against expected.json. Run: python check_results.py"""
import csv
import json
import sys
from pathlib import Path

out_dir = sys.argv[1] if len(sys.argv) > 1 else "invoices_out"
expected = json.loads(Path("expected.json").read_text())
rows = {r["file"]: r for r in csv.DictReader(open(f"{out_dir}/review.csv", encoding="utf-8"))}

checked = wrong = 0
for file, want in expected.items():
    got = rows.get(file)
    if got is None:
        print(f"{file}: MISSING from review.csv")
        wrong += len(want)
        checked += len(want)
        continue
    for key, val in want.items():
        checked += 1
        if (got.get(key) or "") != val:
            wrong += 1
            print(f"{file}: {key}: got {got.get(key)!r}, want {val!r}")

print(f"\n{checked - wrong}/{checked} fields correct across {len(expected)} files")
sys.exit(1 if wrong else 0)
