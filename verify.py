"""Work email verification: input checks, disposable/personal domain, MX, name and company match, email OTP.

Env: VERIFY_KEY (agents' access key, also keys the OTP hashes), BREVO_KEY + OTP_FROM (sends the code),
OTP_TURSO_URL + OTP_TURSO_TOKEN (OTP store; a local otp.db when unset).
Without BREVO_KEY, off Vercel, the code is printed to the server log instead (local testing).
PUBLIC_URL (optional): base of the confirm link in the email; defaults to the agent's request host.
"""
import concurrent.futures, difflib, hashlib, hmac, html, http.client, ipaddress, json, os, re, secrets, socket, ssl
import sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
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
    """Comparable company name. 'Private' is dropped: a company converting to public loses it, and MCA doesn't allow
    two names that differ only by it."""
    s = re.sub(r"\b(pvt|private)\b", "", re.sub(r"\bltd\b", "limited", s.lower().replace("&", " and ")))
    return "".join(re.findall(r"[a-z0-9]+", s))


def mca_lookup(company, search):
    """Find the entered company in the MCA data: exact name match, else up to 3 closest names."""
    core = " ".join(w for w in re.findall(r"\w+", company) if w.lower() not in {"private", "pvt", "limited", "ltd"})
    res = search(core or company)["results"]  # "X Private Limited" must also find "X LIMITED" (converted to public)
    want = norm_company(company)
    exact = next((r for r in res if norm_company(r["CompanyName"]) == want), None)
    pick = lambda r: {k: r[k] for k in ("CIN", "CompanyName", "CompanyStatus", "CompanyStateCode")}
    return {"exact": pick(exact) if exact else None, "similar": [] if exact else [pick(r) for r in res[:3]]}


# --- Company website: does the email domain's site publish the company's CIN? ------------------
# Companies Act s.12(3)(c) requires a company's CIN on its website, so a matching CIN ties the domain to the company.

CIN_RE = re.compile(r"\b[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}\b")
LLPIN_RE = re.compile(r"LLPIN\W{0,5}([A-Z]{3}-[0-9]{4})\b")
PAGES = ["/contact", "/contact-us", "/about", "/about-us", "/privacy-policy", "/terms", "/legal", "/investors"]
LINK_WORDS = re.compile(r"contact|about|privacy|terms|legal|disclaimer|investor|imprint|policy", re.I)


def public_ip(host):
    """An address for host, refusing names that resolve to private/internal addresses (no fetching our own infra)."""
    ips = {a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not ips or not all(ipaddress.ip_address(ip).is_global for ip in ips):
        raise OSError(f"{host} doesn't resolve to a public address")
    return sorted(ips)[0]


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to an already-vetted IP, with the real hostname for TLS, so DNS can't be switched between check and use."""
    def __init__(self, host, ip, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((self._ip, 443), self.timeout),
                                              server_hostname=self.host)


def fetch(url, site, timeout=4):
    """GET an https URL on site (or its subdomains), following up to 4 such redirects -> (final url, html) or None."""
    for _ in range(5):
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == site or host.endswith("." + site)):
            return None
        con = _Pinned(host, public_ip(host), timeout)
        try:
            con.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""),
                        headers={"User-Agent": "Mozilla/5.0 (compatible; MCA-Data-verifier)", "Accept": "text/html"})
            r = con.getresponse()
            if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                url = urllib.parse.urljoin(url, r.getheader("Location"))
                continue
            return (url, r.read(1_500_000).decode("utf-8", "replace")) if r.status == 200 else None
        finally:
            con.close()
    return None


def site_ids(domain, budget=6):
    """CINs/LLPINs printed on the domain's homepage and its contact/legal pages -> {'url', 'ids'} or None if unreachable."""
    site = domain[4:] if domain.startswith("www.") else domain
    pool = concurrent.futures.ThreadPoolExecutor(4)  # more at once gets rate-limited by some sites
    try:
        def safe(url):
            try:
                return fetch(url, site)
            except (OSError, http.client.HTTPException, ValueError):
                return None
        homes = [pool.submit(safe, f"https://{h}/") for h in (site, "www." + site)]
        home = None
        try:
            home = next((f.result() for f in concurrent.futures.as_completed(homes, timeout=budget) if f.result()), None)
        except concurrent.futures.TimeoutError:
            pass
        if not home:
            return None
        base, page = home
        links = [urllib.parse.urljoin(base, html.unescape(h)) for h in re.findall(r'href=["\']([^"\'#]+)', page)
                 if LINK_WORDS.search(h)]
        # Pages likeliest to carry the CIN first: contact, then legal, then investor/about pages.
        rank = lambda u: next((i for i, w in enumerate(("contact", "privacy|terms|legal|disclaimer|imprint|policy",
                                                         "investor")) if re.search(w, u, re.I)), 3)
        urls = sorted(dict.fromkeys(links + [urllib.parse.urljoin(base, p) for p in PAGES]), key=rank)[:10]
        pages = [page]
        done, _ = concurrent.futures.wait([pool.submit(safe, u) for u in urls], timeout=budget * .8)
        pages += [f.result()[1] for f in done if f.result()]
        text = " ".join(pages).upper()
        return {"url": base, "ids": list(dict.fromkeys(CIN_RE.findall(text) + LLPIN_RE.findall(text)))[:10]}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def cin_key(cin):
    """The part of a CIN that never changes: state, year and registration number.
    'U72200KA2013PTC097389' and 'U62099KA2013PLC097389' are the same company after converting to public."""
    return cin[6:12] + cin[15:21] if len(cin) == 21 else cin


def website_check(v, domain, found, mca, companies):
    """'confirmed' (site shows the entered company's CIN), 'other' (CINs of other companies), 'none', 'unreachable'."""
    if found is None:
        return {"verdict": "unreachable", "url": f"https://{domain}/", "companies": []}
    known = {r["CIN"]: r for r in (companies(found["ids"]) if found["ids"] else [])}
    cands = [mca["exact"]] if mca["exact"] else mca["similar"]  # the entered company, or names close to it
    rows, hit = [], False
    for i in found["ids"]:
        same = next((c for c in cands if cin_key(c["CIN"]) == cin_key(i)), None)  # matches even an outdated CIN
        r = known.get(i) or same or {"CIN": i, "CompanyName": None, "CompanyStatus": None}
        rows.append(r)
        hit = hit or bool(same and (mca["exact"] or norm_company(same["CompanyName"]) == norm_company(v["company"])
                                    or company_domain_match(same["CompanyName"], domain) == "strong"))
    return {"verdict": "confirmed" if hit else "other" if rows else "none", "url": found["url"], "companies": rows}


# --- Network: who owns the internet address the customer confirmed from? ----------------------------

GENERIC = {"india", "indian", "services", "service", "technologies", "technology", "tech", "solutions", "systems",
           "software", "global", "international", "consultancy", "consulting", "industries", "enterprises", "group",
           "holdings", "ventures", "network", "networks", "communications", "infotech", "digital", "labs", "bank"}


def _texts(o):
    """Every name/org/description string in an RDAP record (vcard 'fn', 'name', remarks)."""
    if isinstance(o, dict):
        for k, val in o.items():
            if k == "vcardArray":
                yield from (f[3] for f in val[1] if f[0] in ("fn", "org") and isinstance(f[3], str))
            elif k in ("name", "description") and isinstance(val, (str, list)):
                yield from ([val] if isinstance(val, str) else [x for x in val if isinstance(x, str)])
            elif k in ("entities", "remarks"):
                for x in val:
                    yield from _texts(x)


def network_owner(ip):
    """{'network', 'owner'} from the regional registry (RDAP) for a public IP."""
    with urllib.request.urlopen(f"https://rdap.org/ip/{ipaddress.ip_address(ip)}", timeout=6) as r:
        j = json.load(r)
    noise = re.compile(r"^\*|abuse|https?:|@|report|contact|regards|please|-{3,}|troubleshoot", re.I)
    texts = list(dict.fromkeys(t.strip() for t in _texts(j) if t.strip() and len(t) <= 60 and not noise.search(t)))
    return {"network": j.get("name"), "owner": "; ".join(texts[:6])}


ISP = re.compile(r"jio|airtel|bharti|bsnl|mtnl|vodafone|idea cellular|tata (communications|teleservices|play)|hathway|"
                 r"act fibernet|atria convergence|excitel|spectra|you broadband|railtel|sify|gtpl|den networks|alliance broadband",
                 re.I)


def network_match(company, domain, owner_text):
    """True when the network's registered owner names the email domain, or (unless it's a consumer/mobile ISP)
    a distinctive company word. An office leased line usually carries the customer's name; home broadband doesn't."""
    toks = set(re.findall(r"[a-z0-9]+", owner_text.lower()))
    words = {w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in LEGAL and w not in GENERIC and len(w) >= 4}
    label = re.sub(r"[^a-z0-9]", "", domain_label(domain))
    return (len(label) >= 3 and label in toks) or (bool(words & toks) and not ISP.search(owner_text))


def checks(v, search, companies=lambda cins: []):
    domain = v["email"].split("@")[1]
    labels = domain.split(".")
    disposable = any(".".join(labels[i:]) in DISPOSABLE for i in range(len(labels) - 1))  # x.mailinator.com too
    personal = domain in PERSONAL
    bg = concurrent.futures.ThreadPoolExecutor(1)
    site = None if personal or disposable else bg.submit(site_ids, domain)  # slowest check: runs alongside the rest
    bg.shutdown(wait=False)
    try:
        servers, mx_error = mx(domain), None
    except OSError as e:
        servers, mx_error = [], str(e)
    mca = mca_lookup(v["company"], search)
    return {"domain": domain, "disposable": disposable, "personal": personal,
            "mx": servers[:3], "mx_error": mx_error, "name_match": name_match(v["name"], v["email"]),
            "company_domain": "personal email" if personal else company_domain_match(v["company"], domain),
            "mca": mca, "website": site and website_check(v, domain, _result(site), mca, companies)}


def _result(future):
    try:
        return future.result(timeout=10)
    except (OSError, concurrent.futures.TimeoutError):
        return None


# --- OTP ---------------------------------------------------------------------------------------

SCHEMA = ("CREATE TABLE IF NOT EXISTS otp (id TEXT PRIMARY KEY, customer_id TEXT, email TEXT, code_hash TEXT, "
          "link_hash TEXT UNIQUE, company TEXT, created INTEGER, attempts INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, "
          "via TEXT, net TEXT)")


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


def send_email(to, code, link):
    text = (f"Your verification code is {code}\n\nShare it only with the agent verifying your work email.\n\n"
            f"Or, if you're at your office, open this link while connected to the office Wi-Fi and tap Confirm:\n{link}\n\n"
            f"Both expire in {OTP_TTL // 60} minutes. If you didn't expect this, ignore this email.")
    missing = [k for k in ("BREVO_KEY", "OTP_FROM") if not os.environ.get(k)]
    if missing:
        if os.environ.get("VERCEL"):
            raise RuntimeError(f"Email sending isn't set up yet ({' and '.join(missing)} missing).")
        print(f"[dev] OTP for {to}: {link} {code}", file=sys.stderr, flush=True)
        return
    body = json.dumps({"sender": {"name": "MCA Data", "email": os.environ["OTP_FROM"]}, "to": [{"email": to}],
                       "subject": f"{code} is your verification code", "textContent": text}).encode()
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", body,
                                 {"api-key": os.environ["BREVO_KEY"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as e:  # Brevo explains itself, e.g. "sender not valid", "account under validation"
        raise RuntimeError(f"Brevo refused it ({e.code}): {e.read().decode(errors='replace')[:200]}") from None


def start(v, base_url):
    """Email a code and a confirm link to v['email'] -> {'id', 'expires_in'} or {'error'}.

    The agent gets only id; the link token goes only to the inbox, so the agent can't confirm on the customer's behalf.
    """
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
    otp_id, code, link = secrets.token_urlsafe(16), f"{secrets.randbelow(10 ** 6):06d}", secrets.token_urlsafe(16)
    try:
        send_email(v["email"], code, f"{base_url.rstrip('/')}/c/{link}")
    except (RuntimeError, OSError) as e:  # the checks above are still worth showing
        return {"error": f"Couldn't send the code: {e}"}
    db([("INSERT INTO otp (id, customer_id, email, code_hash, link_hash, company, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
         (otp_id, v["customer_id"], v["email"], code_hash(otp_id, code), code_hash("link", link), v["company"], now))])
    return {"id": otp_id, "expires_in": OTP_TTL}


def confirm(otp_id, code):
    """-> {'verified': True, ...} or {'verified': False, 'error', 'attempts_left'}."""
    otp_id, code = str(otp_id), str(code).strip()
    if not re.fullmatch(r"[0-9]{6}", code):
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
        db([("UPDATE otp SET verified = 1, via = 'code' WHERE id = ?", (otp_id,))])
        return {"verified": True, "customer_id": r["customer_id"], "email": r["email"],
                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    left = OTP_TRIES - r["attempts"]
    return {"verified": False, "error": "Wrong code." + ("" if left else " Send a new one."), "attempts_left": left}


def click(link, ip):
    """The customer tapped Confirm on the emailed link: mark verified and record who owns their network."""
    now = int(time.time())
    row, = db([("UPDATE otp SET verified = 1, via = 'link' WHERE link_hash = ? AND created > ? AND verified = 0 "
                "RETURNING id, email, company", (code_hash("link", str(link)), now - OTP_TTL))])
    if not row:
        return {"ok": False, "error": "This link has expired or was already used."}
    r = row[0]
    net = {"ip_kind": "private"}
    try:
        if ipaddress.ip_address(ip).is_global:
            net = network_owner(ip)
            net["match"] = network_match(r["company"], r["email"].split("@")[1], net["owner"] + " " + (net["network"] or ""))
    except (OSError, ValueError) as e:
        net = {"error": str(e)}
    db([("UPDATE otp SET net = ? WHERE id = ?", (json.dumps(net), r["id"]))])
    return {"ok": True}


def status(otp_id):
    """For the agent's page: has the customer confirmed yet, how, and from whose network?"""
    row, = db([("SELECT verified, via, net, customer_id, email FROM otp WHERE id = ?", (str(otp_id),))])
    if not row:
        return {"verified": False}
    r = row[0]
    return {"verified": bool(r["verified"]), "via": r["via"], "customer_id": r["customer_id"], "email": r["email"],
            "network": json.loads(r["net"]) if r["net"] else None}


CONFIRM_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Confirm your work email</title>
<meta name="robots" content="noindex"><style>
:root{--bg:#f5f6f8;--panel:#fff;--text:#0f172a;--muted:#64748b;--accent:#1e40af;--ok:#067647;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--panel:#111827;--text:#e5e7eb;--muted:#94a3b8;--accent:#93b4ff;--ok:#6ee7a8;--bad:#fca5a5}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
.card{background:var(--panel);border-radius:14px;padding:28px 24px;max-width:420px;width:100%;box-shadow:0 4px 16px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 8px}p{color:var(--muted);margin:0 0 20px}
button{width:100%;font:inherit;font-weight:600;border:0;border-radius:10px;padding:14px;background:var(--accent);color:var(--panel);cursor:pointer}
button:disabled{opacity:.6}.ok{color:var(--ok)}.bad{color:var(--bad)}</style></head><body><main class="card">
<h1>Confirm your work email</h1><p id="m">If you can, connect to your office Wi-Fi first. Then tap Confirm.</p>
<button id="b">Confirm</button></main><script>
document.getElementById("b").onclick = async e => {
  e.target.disabled = true; const m = document.getElementById("m");
  try {
    const r = await (await fetch("/api/verify/click", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({link: location.pathname.split("/").pop()})})).json();
    m.className = r.ok ? "ok" : "bad"; m.textContent = r.ok ? "Done. You can close this page and let your agent know." : r.error;
    if (r.ok) e.target.remove(); else e.target.disabled = false;
  } catch (_) { m.className = "bad"; m.textContent = "Network error. Try again."; e.target.disabled = false; }
};
</script></body></html>""".encode()


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
    assert norm_company("Razorpay Software Private Limited") == norm_company("RAZORPAY SOFTWARE LIMITED")
    assert cin_key("U72200KA2013PTC097389") == cin_key("U62099KA2013PLC097389") != cin_key("U62099KA2024PTC188982")
    rz = {"CIN": "U62099KA2013PLC097389", "CompanyName": "RAZORPAY SOFTWARE LIMITED", "CompanyStatus": "Active"}
    w = website_check({"company": "Razorpay Software Limited"}, "razorpay.com",
                      {"url": "https://razorpay.com/", "ids": ["U62099KA2024PTC188982", "U72200KA2013PTC097389"]},
                      {"exact": rz, "similar": []}, lambda ids: [])
    assert w["verdict"] == "confirmed" and w["companies"][1]["CompanyName"] == "RAZORPAY SOFTWARE LIMITED", w
    assert "mailinator.com" in DISPOSABLE and "gmail.com" not in DISPOSABLE
    os.environ.setdefault("VERIFY_KEY", "test")
    assert code_hash("a", "1") != code_hash("a", "2") == code_hash("a", "2")
    assert CIN_RE.findall("CIN: L85110KA1981PLC013115 | x U72200MH2010PTC123456X") == ["L85110KA1981PLC013115"]
    assert LLPIN_RE.findall("LLPIN: AAB-1234 and ISO-9001") == ["AAB-1234"]
    assert network_match("Infosys Limited", "infosys.com", "INFOSYS-NET; Infosys Ltd")
    assert network_match("Tata Consultancy Services Limited", "tcs.com", "TCS-IN; Tata Communications")
    assert not network_match("Tata Consultancy Services Limited", "tcs.com", "RELIANCEJIO-IN; Reliance Jio Infocomm Limited")
    assert not network_match("Acme India Services Pvt Ltd", "acme.io", "Bharti Airtel Ltd. Telemedia Services")
    assert not network_match("Tata Consultancy Services Limited", "tcs.com", "TATACOMM-IN; Tata Communications Ltd")
    for bad in ("localhost", "127.0.0.1", "169.254.169.254", "10.0.0.1"):
        try:
            public_ip(bad)
            raise AssertionError(bad)
        except OSError:
            pass
    assert fetch("http://example.com/", "example.com") is None and fetch("https://evil.com/", "example.com") is None
    print("ok")


if __name__ == "__main__":
    test()
