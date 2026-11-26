#!/usr/bin/env python3
"""Company search UI over roc.db.  Run: python3 app.py  ->  http://127.0.0.1:8000

Env: PORT, HOST (default 127.0.0.1), DATA_DIR (where roc.db lives).
With TURSO_URL + TURSO_TOKEN set it queries Turso instead (how it runs on Vercel, via api/index.py).
"""
import functools, json, os, re, sqlite3, sys, time
import turso
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.environ.get("DATA_DIR", HERE), "roc.db")
PAGE = open(os.path.join(HERE, "index.html"), "rb").read()
LIST_COLS = "CIN, CompanyName, CompanyStatus, CompanyStateCode, CompanyClass, CompanyRegistrationdate_date, updated_at"


def words_of(q):
    return re.findall(r"\w+", q.lower())


def fts_query(words):
    """['tata', 'motors'] -> '"tata"* "motors"*' (every word, prefix match)."""
    return " ".join(f'"{w}"*' for w in words)


def typos(a, b):
    """Edit distance counting insert, delete, substitute and swap-two-neighbours as 1 each."""
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def max_typos(word):
    return 1 if len(word) <= 6 else 2


def closest(word, cands):
    """Best match for word among [(term, company_count)]: fewest typos, then most companies.

    Terms that merely extend word are skipped: the prefix search already finds them
    (a half-typed "relia" must not become "reliant").
    """
    best = None
    for term, n in cands:
        if term.startswith(word):
            continue
        d = typos(word, term)
        if d <= max_typos(word) and (best is None or (d, -n) < best[:2]):
            best = (d, -n, term)
    return best and best[2]


@functools.lru_cache(maxsize=20000)
def count(word):
    """Companies with a name word starting with word (a half-typed "relia" counts all of "reliance")."""
    return query("SELECT coalesce(sum(n), 0) n FROM vocab WHERE term >= ? AND term < ?",
                 (word, word + "\U0010ffff"))[0]["n"]


def correct(word):
    """A similar word used by >=10x as many companies (likely what was meant), else word itself.

    Company names contain real misspellings ("RELIENCE"), so a typo usually still matches
    something; commonness, not existence, is what tells a typo from the intended word.
    """
    if len(word) < 4:
        return word
    n = count(word)
    if n >= 1000:  # already common
        return word
    # ponytail: only words with the same first letter and length ±2 are considered, so a typo
    # in the first letter isn't corrected. Upgrade path: trigram index over vocab.
    k = max_typos(word)
    cands = query("SELECT term, n FROM fix WHERE c = ? AND l BETWEEN ? AND ? AND n >= ?",
                  (word[0], len(word) - k, len(word) + k, 10 * max(n, 1)))
    return closest(word, [(c["term"], c["n"]) for c in cands]) or word


def query(sql, args, max_seconds=None):
    if os.environ.get("TURSO_URL"):
        return turso.run([(sql, args)], timeout=25)[0]  # a cold first read can take >10 s
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # fresh per request: picks up rebuilt DB
    db.row_factory = sqlite3.Row
    if max_seconds:  # abort runaway queries (raises sqlite3.OperationalError "interrupted")
        deadline = time.monotonic() + max_seconds
        db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


BROAD = 500_000  # ranking every match costs ~1.5 us each: "india" (179k) 0.3 s, "private" (2.3M) 3.6 s


def name_search(words, state):
    cols = ", ".join("c." + x for x in LIST_COLS.split(", "))
    # When even the rarest word matches over BROAD companies, return 50 matches unranked (fast).
    order = "ORDER BY rank" if min(count(w) for w in words) <= BROAD else ""
    return query(f"SELECT {cols} FROM names JOIN companies c ON c.rowid = names.rowid "
                 f"WHERE names MATCH ? AND (? = '' OR c.CompanyStateCode = ?) {order} LIMIT 50",
                 (fts_query(words), state, state), max_seconds=1.5)


def search(q, state=""):
    """{'results': [...], 'corrected': 'tata motors' if typo-corrected results lead, else None}.

    Corrected matches come first, then literal matches, so an exact (rare) name is never lost.
    """
    q = q.strip()
    if not q:
        return {"results": [], "corrected": None}
    try:
        return _search(q, state)
    except sqlite3.OperationalError as e:
        if "interrupted" not in str(e):
            raise
        return {"results": [], "corrected": None, "too_broad": True}


def _search(q, state, corrected=None):
    # An exact CIN is unambiguous, so it ignores the state filter.
    hits = query(f"SELECT {LIST_COLS} FROM companies WHERE CIN = ?", (q.upper(),))
    words = words_of(q)
    if words:
        fixed = [correct(w) for w in words]
        changed = [i for i, (a, b) in enumerate(zip(words, fixed)) if a != b]
        # Try every correction together, then one at a time: a rare-but-right word ("hdfc")
        # can get "corrected" too, and must not sink the real fix ("bnak" -> "bank").
        tries = [fixed] + ([words[:i] + [fixed[i]] + words[i + 1:] for i in changed] if len(changed) > 1 else [])
        for t in (tries if changed else []):
            found = name_search(t, state)
            if found:
                hits += found
                corrected = " ".join(t)
                break
        seen = {r["CIN"] for r in hits}
        hits += [r for r in name_search(words, state) if r["CIN"] not in seen]
    return {"results": hits, "corrected": corrected}


def meta():
    m = {r["k"]: r["v"] for r in query("SELECT k, v FROM meta", ())}
    return {"n": int(m["count"]), "built": m["built"], "states": json.loads(m["states"])}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/search":
                body = search(p.get("q", ""), p.get("state", ""))
            elif u.path == "/api/company":
                body = (query("SELECT c.*, k.at checked_at FROM companies c LEFT JOIN checked k "
                              "ON k.state = c.CompanyStateCode WHERE c.CIN = ?", (p.get("cin", ""),)) or [None])[0]
            elif u.path == "/api/meta":
                body = meta()
            else:
                return self.send(404, b"not found", "text/plain")
        except (sqlite3.Error, RuntimeError, OSError) as e:
            return self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        self.send(200, json.dumps(body).encode(), "application/json")

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test():
    assert fts_query(words_of('Tata "Motors" ltd.')) == '"tata"* "motors"* "ltd"*'
    assert fts_query(words_of('*) OR ("')) == '"or"*'
    assert fts_query(words_of("  ")) == ""
    assert typos("bnak", "bank") == 1 and typos("relience", "resilience") == 2 and typos("abc", "abc") == 0
    cands = [("infosys", 280), ("infotech", 9000), ("infosis", 1)]
    assert closest("infosyss", cands) == "infosys"           # fewer typos beats commoner term
    assert closest("mottors", [("motors", 4247), ("mottos", 3)]) == "motors"  # tie -> commoner
    assert closest("bnak", [("bnk", 20), ("bank", 738)]) == "bank"
    assert closest("xyz", cands) is None
    assert closest("relience", [("resilience", 50), ("reliance", 5000)]) == "reliance"
    assert closest("relia", [("reliant", 900), ("reliance", 5000)]) is None
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        test()
    else:
        host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8000))
        print(f"http://{host}:{port}", flush=True)
        ThreadingHTTPServer((host, port), H).serve_forever()
