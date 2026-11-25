"""Minimal Turso client over its HTTP API (stdlib only). Env: TURSO_URL, TURSO_TOKEN."""
import json, os, time, urllib.request


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _val(c):
    return int(c["value"]) if c["type"] == "integer" else c.get("value")


def run(stmts, timeout=30, attempts=1):
    """[(sql, args), ...] run in order on one connection -> list of [row dict, ...] per statement."""
    url = os.environ["TURSO_URL"].replace("libsql://", "https://") + "/v2/pipeline"
    body = json.dumps({"requests": [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}}
                                    for s, args in stmts] + [{"type": "close"}]}).encode()
    req = urllib.request.Request(url, body, {"Authorization": "Bearer " + os.environ["TURSO_TOKEN"],
                                             "Content-Type": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                results = json.load(r)["results"][:-1]
            break
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(10 * 2 ** attempt)
    out = []
    for r in results:
        if r["type"] != "ok":
            raise RuntimeError(r["error"]["message"])
        res = r["response"]["result"]
        cols = [c["name"] for c in res["cols"]]
        out.append([dict(zip(cols, map(_val, row))) for row in res["rows"]])
    return out
