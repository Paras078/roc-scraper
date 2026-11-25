#!/usr/bin/env python3
"""Rebuild roc.db from every data/*.csv (full-text index on company name).

Per company it also keeps:
  checked_at  - the run that last fetched it (its state CSV's write time)
  updated_at  - the run that last *changed* it: carried over from the previous roc.db while
                the record's fields are identical, otherwise that run's time
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


def rows_of(path):
    run = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            # skip broken rows (e.g. a name with a raw newline splits one record in two)
            if r.get("CIN") and r.get("CompanyName") and (r.get("CompanyROCcode") or "").startswith("ROC"):
                vals = tuple(r.get(c) or "" for c in SRC)
                yield vals + (hashlib.md5("\x1f".join(vals).encode()).hexdigest(), run, run)


def build(files, path, old=None):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE companies ({', '.join(COLS)}, row_hash, updated_at, checked_at, PRIMARY KEY (CIN))")
    db.execute("CREATE VIRTUAL TABLE names USING fts5(CompanyName, content='companies', content_rowid='rowid')")
    ins = f"INSERT OR REPLACE INTO companies VALUES ({','.join('?' * (len(COLS) + 3))})"
    for f in files:
        db.executemany(ins, rows_of(f))
        print(f"  loaded {os.path.basename(f)}", flush=True)
    if old and os.path.exists(old):
        db.execute("ATTACH ? AS old", (old,))
        if any(c[1] == "row_hash" for c in db.execute("PRAGMA old.table_info(companies)")):
            db.execute("UPDATE companies SET updated_at = o.updated_at FROM old.companies o "
                       "WHERE o.CIN = companies.CIN AND o.row_hash = companies.row_hash")
        db.commit()
        db.execute("DETACH old")
    db.execute("INSERT INTO names(names) VALUES ('rebuild')")
    # Every distinct name word + how many companies use it; app.py corrects typos against this.
    db.execute("CREATE VIRTUAL TABLE temp.v USING fts5vocab(main, names, row)")
    db.execute("CREATE TABLE vocab (term TEXT PRIMARY KEY, n INT) WITHOUT ROWID")
    db.execute("INSERT INTO vocab SELECT term, doc FROM temp.v")
    db.execute("CREATE TABLE meta (k PRIMARY KEY, v)")
    db.execute("INSERT INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
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
                w.writerow([cin, "N " + cin, "ROC X"] + [""] * 8 + [status] + [""] * 4)
        os.utime(f, (when, when))

    t1, t2 = time.mktime((2026, 11, 15, 10, 0, 0, 0, 0, -1)), time.mktime((2026, 11, 22, 10, 0, 0, 0, 0, -1))
    write([("A", "Active"), ("B", "Active")], t1)
    build([f], v1)
    write([("A", "Active"), ("B", "Strike Off"), ("C", "Active")], t2)
    build([f], v2, old=v1)
    got = {r[0]: r[1:] for r in sqlite3.connect(v2).execute("SELECT CIN, updated_at, checked_at FROM companies")}
    assert got["A"] == ("2026-11-15 10:00", "2026-11-22 10:00"), got  # unchanged: keeps old date
    assert got["B"] == ("2026-11-22 10:00", "2026-11-22 10:00"), got  # changed
    assert got["C"] == ("2026-11-22 10:00", "2026-11-22 10:00"), got  # new
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
