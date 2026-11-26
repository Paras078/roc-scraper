"""Work email verification: input checks, disposable/personal domain, MX, name and company match, email OTP.

Env: VERIFY_KEY (agents' access key, also keys the OTP hashes), BREVO_KEY + OTP_FROM (sends the code),
OTP_TURSO_URL + OTP_TURSO_TOKEN (OTP store; a local otp.db when unset).
Without BREVO_KEY, off Vercel, the code is printed to the server log instead (local testing).
"""
import difflib, hashlib, hmac, json, os, re, secrets, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
import turso

HERE = os.path.dirname(os.path.abspath(__file__))
DISPOSABLE = set(open(os.path.join(HERE, "disposable.txt")).read().split())
PERSONAL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "rocketmail.com",
            "outlook.com", "outlook.in", "hotmail.com", "hotmail.co.in", "live.com", "live.in", "msn.com",
            "icloud.com", "me.com", "mac.com", "aol.com", "rediffmail.com", "rediff.com", "protonmail.com",
            "proton.me", "pm.me", "zoho.com", "zohomail.in", "gmx.com", "gmx.net", "mail.com", "yandex.com",
            "tutanota.com", "fastmail.com", "hey.com", "inbox.com", "mail.ru", "qq.com", "163.com"}
ROLE = {"info", "hr", "admin", "accounts", "account", "contact", "support", "sales", "office", "careers", "jobs",
        "team", "hello", "mail", "noreply", "no-reply", "enquiry", "enquiries", "help", "billing", "finance",
        "marketing", "legal", "compliance", "ops", "operations", "service", "care", "customercare", "webmaster"}
LEGAL = {"private", "pvt", "limited", "ltd", "llp", "opc", "the", "and", "of", "co", "company", "inc", "corp"}

CUSTOMER_ID = re.compile(r"[0-9]{1,6}")  # not \d: that also accepts non-ASCII digits
EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                   r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
NAME = re.compile(r"[^\W\d_](?:[^\W\d_]|[ .'-]){0,79}")
COMPANY = re.compile(r"[\w&().,'/ -]{2,120}")

OTP_TTL, OTP_TRIES, RESEND_AFTER, PER_EMAIL_HOUR, PER_DAY = 600, 5, 60, 5, 250  # Brevo free: 300 emails/day


def validate(d):
    """{'customer_id', 'name', 'company', 'email'} -> (clean dict, {field: error})."""
    v = {k: str(d.get(k, "")).strip() for k in ("customer_id", "name", "company", "email")}
    v["email"] = v["email"].lower()
    errors = {}
    if not CUSTOMER_ID.fullmatch(v["customer_id"]):
        errors["customer_id"] = "Digits only, 1 to 6 of them."
    if not NAME.fullmatch(v["name"]):
        errors["name"] = "Letters, spaces, dots, hyphens and apostrophes only."
    if not COMPANY.fullmatch(v["company"]) or not re.search(r"[^\W\d_]", v["company"]):
        errors["company"] = "2 to 120 characters, including at least one letter."
    if len(v["email"]) > 254 or not EMAIL.fullmatch(v["email"]) or len(v["email"].split("@")[0]) > 64:
        errors["email"] = "Not a valid email address."
    return v, errors


def mx(domain):
    """Mail servers for domain via DNS-over-HTTPS (stdlib only); [] if it can't receive mail."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": domain, "type": "MX"})
    with urllib.request.urlopen(url, timeout=5) as r:
        ans = json.load(r).get("Answer", [])
    hosts = [a["data"].split()[-1].rstrip(".").lower() for a in ans if a.get("type") == 15]
    return [h for h in hosts if h]  # a "null MX" (0 .) means the domain accepts no mail


def name_match(name, email):
    """'strong' (every name part, or parts + initials), 'partial' (some part), 'none', or 'role' (hr@, info@...)."""
    local = email.split("@")[0].split("+")[0].lower()
    if local in ROLE or re.sub(r"[^a-z]", "", local) in ROLE:
        return "role"
    parts = re.findall(r"[a-z]+", local)
    rest = "".join(parts)
    toks = re.findall(r"[a-z]+", name.lower())
    full = []
    for t in toks:
        if len(t) >= 3 and (t in rest or any(len(p) >= 4 and difflib.SequenceMatcher(None, t, p).ratio() >= .8
                                              for p in parts)):
            full.append(t)
            rest = rest.replace(t, "", 1)
    initials = [t for t in toks if t not in full and t[0] in rest]
    if full and len(full) + len(initials) == len(toks):
        return "strong"
    return "partial" if full else "none"


def domain_label(domain):
    """'mail.tcs.co.in' -> 'tcs'."""
    p = domain.split(".")
    return p[-3] if len(p) >= 3 and p[-2] in {"co", "com", "net", "org", "gov", "ac", "edu", "res"} else p[-2]


def company_domain_match(company, domain):
    """Does the email domain look like the company's? 'strong' / 'partial' / 'none' (a heuristic, not proof)."""
    label = re.sub(r"[^a-z0-9]", "", domain_label(domain))
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in LEGAL]
    if not words or not label:
        return "none"
    joined, initials = "".join(words), "".join(w[0] for w in words)
    if label in (joined, initials if len(initials) >= 2 else None) or (len(label) >= 4 and label == words[0]):
        return "strong"
    if label in words or (len(label) >= 4 and (label in joined or joined in label)) \
            or any(len(w) >= 4 and w in label for w in words) \
            or difflib.SequenceMatcher(None, label, joined).ratio() >= .8:
        return "partial"
    return "none"


def norm_company(s):
    s = re.sub(r"\bpvt\b", "private", re.sub(r"\bltd\b", "limited", s.lower().replace("&", " and ")))
    return "".join(re.findall(r"[a-z0-9]+", s))


def mca_lookup(company, search):
    """Find the entered company in the MCA data: exact name match, else up to 3 closest names."""
    res = search(company)["results"]
    want = norm_company(company)
    exact = next((r for r in res if norm_company(r["CompanyName"]) == want), None)
    pick = lambda r: {k: r[k] for k in ("CIN", "CompanyName", "CompanyStatus", "CompanyStateCode")}
    return {"exact": pick(exact) if exact else None, "similar": [] if exact else [pick(r) for r in res[:3]]}


def checks(v, search):
    domain = v["email"].split("@")[1]
    labels = domain.split(".")
    disposable = any(".".join(labels[i:]) in DISPOSABLE for i in range(len(labels) - 1))  # x.mailinator.com too
    personal = domain in PERSONAL
    try:
        servers, mx_error = mx(domain), None
    except OSError as e:
        servers, mx_error = [], str(e)
    return {"domain": domain, "disposable": disposable, "personal": personal,
            "mx": servers[:3], "mx_error": mx_error, "name_match": name_match(v["name"], v["email"]),
            "company_domain": "personal email" if personal else company_domain_match(v["company"], domain),
            "mca": mca_lookup(v["company"], search)}


# --- OTP ---------------------------------------------------------------------------------------

SCHEMA = ("CREATE TABLE IF NOT EXISTS otp (id TEXT PRIMARY KEY, customer_id TEXT, email TEXT, code_hash TEXT, "
          "created INTEGER, attempts INTEGER DEFAULT 0, verified INTEGER DEFAULT 0)")


def db(stmts):
    stmts = [(SCHEMA, ())] + stmts
    if os.environ.get("OTP_TURSO_URL"):
        return turso.run(stmts, timeout=15, url=os.environ["OTP_TURSO_URL"],
                         token=os.environ["OTP_TURSO_TOKEN"])[1:]
    con = sqlite3.connect(os.path.join(os.environ.get("DATA_DIR", HERE), "otp.db"))
    con.row_factory = sqlite3.Row
    with con:
        out = [[dict(r) for r in con.execute(s, a)] for s, a in stmts]
    con.close()
    return out[1:]


def code_hash(otp_id, code):
    return hmac.new(os.environ["VERIFY_KEY"].encode(), f"{otp_id}:{code}".encode(), hashlib.sha256).hexdigest()


def send_email(to, code):
    text = (f"Your verification code is {code}\n\nIt expires in {OTP_TTL // 60} minutes. Share it only with the "
            "agent verifying your work email. If you didn't expect this, ignore this email.")
    missing = [k for k in ("BREVO_KEY", "OTP_FROM") if not os.environ.get(k)]
    if missing:
        if os.environ.get("VERCEL"):
            raise RuntimeError(f"Email sending isn't set up yet ({' and '.join(missing)} missing).")
        print(f"[dev] OTP for {to}: {code}", file=sys.stderr, flush=True)
        return
    body = json.dumps({"sender": {"name": "MCA Data", "email": os.environ["OTP_FROM"]}, "to": [{"email": to}],
                       "subject": f"{code} is your verification code", "textContent": text}).encode()
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", body,
                                 {"api-key": os.environ["BREVO_KEY"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as e:  # Brevo explains itself, e.g. "sender not valid", "account under validation"
        raise RuntimeError(f"Brevo refused it ({e.code}): {e.read().decode(errors='replace')[:200]}") from None


def start(v):
    """Send a code to v['email'] -> {'id', 'expires_in'} or {'error'} when rate limited."""
    now = int(time.time())
    mine, day = db([("DELETE FROM otp WHERE created < ?", (now - 86400,)),
                    ("SELECT count(*) n, coalesce(max(created), 0) last FROM otp WHERE email = ? AND created > ?",
                     (v["email"], now - 3600)),
                    ("SELECT count(*) n FROM otp", ())])[1:]
    if now - mine[0]["last"] < RESEND_AFTER:
        return {"error": f"A code was just sent. Wait {RESEND_AFTER - (now - mine[0]['last'])} s to resend."}
    if mine[0]["n"] >= PER_EMAIL_HOUR:
        return {"error": "Too many codes sent to this address in the last hour. Try again later."}
    if day[0]["n"] >= PER_DAY:
        return {"error": "Daily email limit reached. Try again tomorrow."}
    otp_id, code = secrets.token_urlsafe(16), f"{secrets.randbelow(10 ** 6):06d}"
    try:
        send_email(v["email"], code)
    except (RuntimeError, OSError) as e:  # the checks above are still worth showing
        return {"error": f"Couldn't send the code: {e}"}
    db([("INSERT INTO otp (id, customer_id, email, code_hash, created) VALUES (?, ?, ?, ?, ?)",
         (otp_id, v["customer_id"], v["email"], code_hash(otp_id, code), now))])
    return {"id": otp_id, "expires_in": OTP_TTL}


def confirm(otp_id, code):
    """-> {'verified': True, ...} or {'verified': False, 'error', 'attempts_left'}."""
    otp_id, code = str(otp_id), str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        return {"verified": False, "error": "The code is 6 digits."}
    now = int(time.time())
    # Count the attempt first, atomically, so parallel guesses can't exceed OTP_TRIES.
    row, = db([("UPDATE otp SET attempts = attempts + 1 WHERE id = ? AND attempts < ? AND created > ? "
                "AND verified = 0 RETURNING code_hash, attempts, customer_id, email", (otp_id, OTP_TRIES, now - OTP_TTL))])
    if not row:
        why, = db([("SELECT verified, attempts, created FROM otp WHERE id = ?", (otp_id,))])
        if why and why[0]["verified"]:
            return {"verified": False, "error": "This code was already used."}
        if why and why[0]["created"] > now - OTP_TTL:
            return {"verified": False, "error": "Too many wrong codes. Send a new one.", "attempts_left": 0}
        return {"verified": False, "error": "The code expired. Send a new one."}
    r = row[0]
    if hmac.compare_digest(r["code_hash"], code_hash(otp_id, code)):
        db([("UPDATE otp SET verified = 1 WHERE id = ?", (otp_id,))])
        return {"verified": True, "customer_id": r["customer_id"], "email": r["email"],
                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    left = OTP_TRIES - r["attempts"]
    return {"verified": False, "error": "Wrong code." + ("" if left else " Send a new one."), "attempts_left": left}


def test():
    v, e = validate({"customer_id": "123456", "name": "Paras Arora", "company": "Tata Motors Ltd", "email": "A@B.co"})
    assert not e and v["email"] == "a@b.co"
    for bad in ("", "1234567", "12a", "-1", "1.5", "١٢٣"):
        assert "customer_id" in validate({"customer_id": bad})[1], bad
    for bad in ("a@b", "a b@c.com", "a@-b.com", "@b.com", "a@b.c", "a..b@c.com", "x" * 65 + "@b.com"):
        assert "email" in validate({"email": bad})[1], bad
    assert "name" in validate({"name": "R2D2"})[1] and "name" not in validate({"name": "Anne-Marie O'Neil"})[1]
    assert "company" in validate({"company": "123"})[1] and "company" in validate({"company": "<script>"})[1]
    for local, want in [("paras.arora", "strong"), ("parasarora", "strong"), ("p.arora", "strong"),
                        ("arora.p", "strong"), ("paras", "partial"), ("paars.arora", "strong"),
                        ("arora", "partial"), ("jdoe", "none"), ("hr", "role"), ("info+x", "role")]:
        assert name_match("Paras Arora", local + "@x.com") == want, (local, name_match("Paras Arora", local + "@x.com"))
    assert name_match("Paras Kumar Arora", "paras.arora@x.com") == "partial"
    for co, dom, want in [("Tata Consultancy Services Limited", "tcs.com", "strong"),
                          ("Infosys Limited", "infosys.com", "strong"),
                          ("Tata Motors Ltd", "tatamotors.com", "strong"),
                          ("Flipkart Internet Private Limited", "flipkart.com", "strong"),
                          ("Reliance Jio Infocomm Limited", "jio.com", "partial"),
                          ("Wipro Limited", "mail.wipro.co.in", "strong"),
                          ("Si Creva Capital Services", "kissht.com", "none")]:
        assert company_domain_match(co, dom) == want, (co, dom, company_domain_match(co, dom))
    assert norm_company("Tata Motors Pvt. Ltd.") == norm_company("TATA MOTORS PRIVATE LIMITED")
    assert "mailinator.com" in DISPOSABLE and "gmail.com" not in DISPOSABLE
    os.environ.setdefault("VERIFY_KEY", "test")
    assert code_hash("a", "1") != code_hash("a", "2") == code_hash("a", "2")
    print("ok")


if __name__ == "__main__":
    test()
