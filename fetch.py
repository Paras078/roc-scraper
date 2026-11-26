#!/usr/bin/env python3
"""Fetch today's states from data.gov.in RoC company master data into data/<state>.csv.

Every state is on exactly one weekday, so each is refreshed once a week.
Usage: fetch.py [state ...]   (no args = today's states + any not fetched in STALE_DAYS, e.g. Mac was off)
       fetch.py --today         (today's states only, e.g. on a fresh CI machine)   |   fetch.py --test
API key: $DATAGOV_KEY, else ~/.datagov_key.
"""
import csv, datetime, json, os, re, sys, tempfile, time, urllib.parse, urllib.request

RESOURCE = "4dbe5667-7b6b-41d7-82af-211562424d9a"
BASE = f"https://api.data.gov.in/resource/{RESOURCE}"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("DATA_DIR", HERE), "data")
LIMIT = 1000
# Exact CompanyStateCode values the API filters on (checked 2026-11-25: together they cover all
# 3,738,721 records). Days balanced by record count, ~490k/day; Maharashtra and Delhi get their own.
DAYS = [  # Monday .. Sunday
    ["maharashtra"],
    ["delhi"],
    ["west bengal", "madhya pradesh", "bihar", "goa", "himachal pradesh"],
    ["uttar pradesh", "rajasthan", "orissa", "chandigarh", "chattisgarh", "arunachal pradesh",
     "andaman and nicobar islands", "ladakh", "sikkim"],
    ["karnataka", "kerala", "punjab", "jharkhand", "manipur", "tripura", "nagaland", "daman and diu",
     "lakshadweep"],
    ["tamil nadu", "haryana", "andhra pradesh", "uttarakhand", "pondicherry", "meghalaya",
     "dadra & nagar haveli", "mizoram"],
    ["gujarat", "telangana", "assam", "jammu & kashmir"],
]
STATES = [s for day in DAYS for s in day]
STALE_DAYS = 8  # a state is due weekly; older than this means its day was missed


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def csv_path(state):
    return os.path.join(DATA, re.sub(r"\W+", "_", state) + ".csv")


def stale(state, now=None):
    """Never fetched, or last good fetch older than STALE_DAYS (the file is only replaced on success)."""
    p = csv_path(state)
    return not os.path.exists(p) or (now or time.time()) - os.path.getmtime(p) > STALE_DAYS * 86400


def get(key, state, offset):
    # Sorted by CIN so offset paging is deterministic (no row seen twice or skipped).
    q = urllib.parse.urlencode({"api-key": key, "format": "json", "limit": LIMIT, "offset": offset,
                                "filters[CompanyStateCode]": state, "sort[CIN]": "asc"})
    for attempt in range(6):
        try:
            # The API stalls requests carrying Python's default User-Agent.
            req = urllib.request.Request(f"{BASE}?{q}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r:  # pages normally take 2-5s
                return json.load(r)
        except Exception as e:  # 429s and flaky gateway errors happen; back off up to ~15 min
            if attempt == 5:
                raise
            wait = 30 * 2 ** attempt
            log(f"  {state} @{offset}: {e}; retry in {wait}s")
            time.sleep(wait)


def fetch_state(key, state):
    first = get(key, state, 0)
    total = int(first.get("total") or 0)
    if total == 0:  # never overwrite good data with an empty file
        raise RuntimeError(f"API returned 0 records ({first.get('message') or first.get('error')})")
    cols = [f["id"] for f in first["field"]]
    os.makedirs(DATA, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            cins, page = set(), first
            while True:
                recs = page.get("records") or []
                w.writerows(recs)
                cins.update(r.get("CIN") for r in recs)
                if not recs or len(cins) >= total:
                    break
                page = get(key, state, len(cins))
        if len(cins) != total:  # short or duplicated pages: keep last week's file instead
            raise RuntimeError(f"got {len(cins)} distinct companies, API says {total}")
        os.replace(tmp, csv_path(state))
    except BaseException:
        os.unlink(tmp)
        raise
    return total


def test():
    assert len(STATES) == 37 and len(set(STATES)) == 37 and len(DAYS) == 7
    assert csv_path("tamil nadu").endswith("/tamil_nadu.csv")  # matches the seeded file names
    assert csv_path("jammu & kashmir").endswith("/jammu_kashmir.csv")
    global DATA
    DATA, real = tempfile.mkdtemp(), DATA
    open(csv_path("goa"), "w").close()
    assert not stale("goa") and stale("goa", time.time() + 9 * 86400) and stale("delhi")  # fresh, overdue, missing
    DATA = real
    print("ok")


def main(args):
    if args == ["--test"]:
        return test()
    key = os.environ.get("DATAGOV_KEY") or open(os.path.expanduser("~/.datagov_key")).read().strip()
    # Overdue states too: a missed day (Mac off/asleep) is caught up next run, not a week later.
    args = [a.strip().lower() for a in args if a.strip()]  # "Goa, west bengal" from the CI form
    today = DAYS[datetime.date.today().weekday()]
    states = today if args == ["--today"] else args or today + [s for s in STATES if stale(s)]
    states = list(dict.fromkeys(states))
    log(f"run start: {states}")
    failed = []
    for s in states:
        t = time.time()
        try:
            log(f"{s}: {fetch_state(key, s)} companies in {time.time() - t:.0f}s")
        except Exception as e:
            log(f"{s}: FAILED {e}")
            failed.append(s)
    log(f"run end: {len(states) - len(failed)} ok, {len(failed)} failed {failed or ''}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
