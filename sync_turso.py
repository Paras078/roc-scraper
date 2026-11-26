#!/usr/bin/env python3
"""Push freshly fetched data/*.csv into Turso, writing only what changed.

Per state: new or changed companies are upserted with updated_at = this run (triggers keep the
name index in step), companies gone from the source are deleted, checked.at = this run.
Unchanged companies cost a read, not a write, which keeps us inside Turso's free write quota.
Usage: sync_turso.py [csv ...]   (default: every data/*.csv newer than what Turso last got for that state,
       so a failed sync is retried next run)   Env: TURSO_URL, TURSO_TOKEN
"""
import csv, glob, os, sys, time
import turso
from build_db import COLS, DIR, rows_of, run_time

CHUNK = 200  # statements per HTTP request


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def run(stmts):
    return turso.run(stmts, timeout=120, attempts=4)


def existing(state):
    """{CIN: row_hash} already in Turso for state, paged by CIN."""
    out, last = {}, ""
    while True:
        rows = run([("SELECT CIN, row_hash FROM companies WHERE CompanyStateCode = ? AND CIN > ? "
                     "ORDER BY CIN LIMIT 50000", (state, last))])[0]
        out.update((r["CIN"], r["row_hash"]) for r in rows)
        if len(rows) < 50000:
            return out
        last = rows[-1]["CIN"]


def apply(stmts):
    for i in range(0, len(stmts), CHUNK):
        run([("BEGIN", ())] + stmts[i:i + CHUNK] + [("COMMIT", ())])


def sync(path):
    seen = {}
    rows = {r[0]: r for r in rows_of(path, seen)}
    if not seen:
        raise RuntimeError(f"{path}: no valid rows")
    (state, at), = seen.items()  # one state per file
    old = existing(state)
    cols = COLS + ["row_hash", "updated_at"]
    upsert = (f"INSERT INTO companies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
              f"ON CONFLICT (CIN) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols[1:])}")
    changed = [(upsert, r) for cin, r in rows.items() if old.get(cin) != r[-2]]
    gone = [("DELETE FROM companies WHERE CIN = ?", (cin,)) for cin in old.keys() - rows.keys()]
    apply(changed + gone)
    new = len(rows.keys() - old.keys())
    run([("INSERT INTO checked VALUES (?, ?) ON CONFLICT (state) DO UPDATE SET at = excluded.at", (state, at)),
         ("UPDATE meta SET v = v + ? WHERE k = 'count'", (new - len(gone),)),
         ("UPDATE meta SET v = (SELECT json_group_array(state) FROM (SELECT state FROM checked ORDER BY 1)) "
          "WHERE k = 'states'", ()),
         ("UPDATE meta SET v = ? WHERE k = 'built'", (time.strftime("%Y-%m-%d %H:%M"),))])
    log(f"{state}: {len(rows)} companies, {len(changed) - new} changed, {new} new, {len(gone)} removed")


def pending():
    """CSVs fetched after Turso's checked.at for their state (or for a state Turso lacks)."""
    got = {r["state"]: r["at"] for r in run([("SELECT state, at FROM checked", ())])[0]}
    out = []
    for p in sorted(glob.glob(os.path.join(DIR, "data", "*.csv"))):
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            first = next(csv.DictReader(fh), None)
        state = first and first.get("CompanyStateCode")
        if state and run_time(p) > got.get(state, ""):
            out.append(p)
    return out


def main(paths):
    failed = []
    paths = paths or pending()
    log(f"sync start: {len(paths)} state file(s) to push")
    for p in paths:
        try:
            sync(p)
        except Exception as e:
            log(f"{os.path.basename(p)}: FAILED {e}")
            failed.append(p)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
