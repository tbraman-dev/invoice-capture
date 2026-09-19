"""Score invoices_out/review.csv against expected.json, split by read_by path.
Run: python check_results.py [out_dir]"""
import csv
import json
import sys
from pathlib import Path


def split_by_path(expected: dict, review_rows: dict, results_by_file: dict) -> dict:
    """Group the per-field score, avg cost and avg duration by the path each doc
    actually took (results.json's read_by -- not an expected one). Pure function,
    no I/O: expected/review_rows/results_by_file are all {file: dict}."""
    agg = {}
    for file, want in expected.items():
        result = results_by_file.get(file) or {}
        path = result.get("read_by") or "unknown"
        got = review_rows.get(file) or {}
        bucket = agg.setdefault(path, {"docs": 0, "fields": 0, "correct": 0,
                                        "cost": [], "duration": []})
        bucket["docs"] += 1
        for key, val in want.items():
            bucket["fields"] += 1
            if (got.get(key) or "") == val:
                bucket["correct"] += 1
        cost = result.get("cost_usd")
        if cost is not None:
            bucket["cost"].append(cost)
        duration = result.get("duration_ms")
        if duration is not None:
            bucket["duration"].append(duration / 1000)

    out = {}
    for path, b in agg.items():
        out[path] = {
            "docs": b["docs"], "fields": b["fields"], "correct": b["correct"],
            "avg_cost": (sum(b["cost"]) / len(b["cost"])) if b["cost"] else 0.0,
            "avg_duration": (sum(b["duration"]) / len(b["duration"])) if b["duration"] else 0.0,
        }
    return out


def score(out_dir: str = "invoices_out") -> int:
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

    results_path = Path(out_dir) / "results.json"
    if results_path.exists():
        results_by_file = {r["source_file"]: r for r in json.loads(results_path.read_text())}
        split = split_by_path(expected, rows, results_by_file)
        for path in ("text", "vision"):
            s = split.get(path)
            if not s:
                continue
            print(f"{path + ':':7} {s['docs']:2} docs, {s['correct']}/{s['fields']} fields, "
                  f"avg ${s['avg_cost']:.4f}/doc, avg {s['avg_duration']:.1f} s")

    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(score(sys.argv[1] if len(sys.argv) > 1 else "invoices_out"))
