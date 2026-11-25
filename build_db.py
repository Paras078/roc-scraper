#!/usr/bin/env python3
"""Rebuild roc.db from every data/*.csv (full-text index on company name).

Per company it also keeps updated_at: the run that last *changed* it, carried over from the
previous roc.db while the record's fields are identical, otherwise that run's time.
Per state, checked.at is the run that last fetched it (its CSV's write time).
The same schema is what Turso serves; sync_turso.py keeps it current with small writes.
Builds into a temp file and renames it, so the running app never sees a half-built DB.
Usage: build_db.py   |   build_db.py --test
"""
import csv, glob, hashlib, os, re, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get("DATA_DIR", HERE)  # holds data/ and roc.db (a mounted volume when hosted)
DB = os.path.join(DIR, "roc.db")
SRC = ["CIN", "CompanyName", "CompanyROCcode", "CompanyCategory", "CompanySubCategory",
       "CompanyClass", "AuthorizedCapital", "PaidupCapital", "CompanyRegistrationdate_date",
       "Registered_Office_Address", "Listingstatus", "CompanyStatus", "CompanyStateCode",
       "CompanyIndian/Foreign Company", "nic_code", "CompanyIndustrialClassification"]
COLS = [re.sub(r"\W+", "_", c) for c in SRC]  # CompanyIndian/Foreign Company -> CompanyIndian_Foreign_Company

csv.field_size_limit(sys.maxsize)


def run_time(path):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))


def row_hash(vals):
    return hashlib.md5("\x1f".join(vals).encode()).hexdigest()


def rows_of(path, seen=None):
    """(16 source fields..., row_hash, updated_at) per valid row; seen[state] = run time."""
    run = run_time(path)
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            # skip broken rows (e.g. a name with a raw newline splits one record in two)
            if r.get("CIN") and r.get("CompanyName") and (r.get("CompanyROCcode") or "").startswith("ROC"):
                vals = tuple(r.get(c) or "" for c in SRC)
                if seen is not None:
                    seen[r["CompanyStateCode"]] = run
                yield vals + (row_hash(vals), run)


def build(files, path, old=None):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE companies ({', '.join(COLS)}, row_hash, updated_at, PRIMARY KEY (CIN))")
    db.execute("CREATE VIRTUAL TABLE names USING fts5(CompanyName, content='companies', content_rowid='rowid')")
    db.execute("CREATE TABLE checked (state PRIMARY KEY, at)")
    ins = f"INSERT OR REPLACE INTO companies VALUES ({','.join('?' * (len(COLS) + 2))})"
    seen = {}
    for f in files:
        db.executemany(ins, rows_of(f, seen))
        print(f"  loaded {os.path.basename(f)}", flush=True)
    db.executemany("INSERT INTO checked VALUES (?, ?)", seen.items())
    db.execute("CREATE INDEX companies_state ON companies (CompanyStateCode)")
    if old and os.path.exists(old):
        db.execute("ATTACH ? AS old", (old,))
        if any(c[1] == "row_hash" for c in db.execute("PRAGMA old.table_info(companies)")):
            db.execute("UPDATE companies SET updated_at = o.updated_at FROM old.companies o "
                       "WHERE o.CIN = companies.CIN AND o.row_hash = companies.row_hash")
        db.commit()
        db.execute("DETACH old")
    db.execute("INSERT INTO names(names) VALUES ('rebuild')")
    # Keep the name index in step with later row changes (sync_turso.py upserts/deletes).
    db.executescript("""
      CREATE TRIGGER companies_ai AFTER INSERT ON companies BEGIN
        INSERT INTO names(rowid, CompanyName) VALUES (new.rowid, new.CompanyName); END;
      CREATE TRIGGER companies_ad AFTER DELETE ON companies BEGIN
        INSERT INTO names(names, rowid, CompanyName) VALUES ('delete', old.rowid, old.CompanyName); END;
      CREATE TRIGGER companies_au AFTER UPDATE OF CompanyName ON companies BEGIN
        INSERT INTO names(names, rowid, CompanyName) VALUES ('delete', old.rowid, old.CompanyName);
        INSERT INTO names(rowid, CompanyName) VALUES (new.rowid, new.CompanyName); END;""")
    # Every distinct name word + how many companies use it; app.py corrects typos against this.
    # ponytail: not updated by sync_turso.py (new words only matter for typo correction);
    # refreshed whenever the whole DB is rebuilt and re-uploaded.
    db.execute("CREATE VIRTUAL TABLE temp.v USING fts5vocab(main, names, row)")
    db.execute("CREATE TABLE vocab (term TEXT PRIMARY KEY, n INT) WITHOUT ROWID")
    db.execute("INSERT INTO vocab SELECT term, doc FROM temp.v")
    # Typo-fix targets: a correction must be used by >=10 companies, so only those words, keyed
    # by first letter + length so a lookup reads ~1k rows, not the whole letter.
    db.execute("CREATE TABLE fix (c, l, term, n, PRIMARY KEY (c, l, term)) WITHOUT ROWID")
    db.execute("INSERT INTO fix SELECT substr(term, 1, 1), length(term), term, n FROM vocab WHERE n >= 10")
    db.execute("CREATE TABLE meta (k PRIMARY KEY, v)")
    db.execute("INSERT INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
    db.execute("INSERT INTO meta SELECT 'count', count(*) FROM companies")
    db.execute("INSERT INTO meta SELECT 'states', json_group_array(state) FROM (SELECT state FROM checked ORDER BY 1)")
    db.commit()
    db.close()


def test():
    d = tempfile.mkdtemp()
    f, v1, v2 = os.path.join(d, "s.csv"), os.path.join(d, "v1.db"), os.path.join(d, "v2.db")

    def write(rows, when):
        with open(f, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(SRC)
            for cin, status in rows:
                w.writerow([cin, "N " + cin, "ROC X"] + [""] * 8 + [status, "goa"] + [""] * 3)
        os.utime(f, (when, when))

    t1, t2 = time.mktime((2026, 11, 15, 10, 0, 0, 0, 0, -1)), time.mktime((2026, 11, 22, 10, 0, 0, 0, 0, -1))
    write([("A", "Active"), ("B", "Active")], t1)
    build([f], v1)
    write([("A", "Active"), ("B", "Strike Off"), ("C", "Active")], t2)
    build([f], v2, old=v1)
    db = sqlite3.connect(v2)
    got = dict(db.execute("SELECT CIN, updated_at FROM companies"))
    assert got == {"A": "2026-11-15 10:00", "B": "2026-11-22 10:00", "C": "2026-11-22 10:00"}, got  # A unchanged
    assert db.execute("SELECT * FROM checked").fetchall() == [("goa", "2026-11-22 10:00")]
    assert dict(db.execute("SELECT * FROM meta WHERE k != 'built'")) == {"count": 3, "states": '["goa"]'}
    # triggers keep the name index in step with edits
    db.execute("UPDATE companies SET CompanyName = 'ZEBRA' WHERE CIN = 'A'")
    db.execute("DELETE FROM companies WHERE CIN = 'B'")
    assert db.execute("SELECT rowid FROM names WHERE names MATCH 'zebra'").fetchall() == [(1,)]
    assert db.execute("SELECT count(*) FROM names WHERE names MATCH 'n'").fetchone() == (1,)  # only C
    print("ok")


def main():
    if sys.argv[1:] == ["--test"]:
        return test()
    files = sorted(glob.glob(os.path.join(DIR, "data", "*.csv")))
    if not files:
        sys.exit("no CSVs in data/")
    t = time.time()
    build(files, DB + ".tmp", old=DB)
    os.replace(DB + ".tmp", DB)
    n = sqlite3.connect(DB).execute("SELECT count(*) FROM companies").fetchone()[0]
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} roc.db: {n} companies from {len(files)} files in {time.time() - t:.0f}s",
          flush=True)


if __name__ == "__main__":
    main()
