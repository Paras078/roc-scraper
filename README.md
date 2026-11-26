# Company Search (RoC master data)

Search Indian companies by name or CIN, over the Ministry of Corporate Affairs
[RoC company master data](https://www.data.gov.in/resource/registrars-companies-roc-wise-company-master-data)
from data.gov.in (Government Open Data License – India). Python stdlib only.

- `fetch.py` pulls each state from the data.gov.in API once a week (that weekday's states, plus any never fetched).
- `build_db.py` rebuilds `roc.db` (SQLite full-text index), keeping each record's "last updated" date while it is unchanged.
- `sync_turso.py` pushes only the changes from fresh CSVs into the hosted (Turso) copy.
- `app.py` + `index.html` is the search UI: typo tolerant, with a state filter.

## Run locally
    echo YOUR_KEY > ~/.datagov_key && chmod 600 ~/.datagov_key
    python3 fetch.py goa && python3 build_db.py && python3 app.py   # http://127.0.0.1:8000
Self-checks: `python3 fetch.py --test`, `build_db.py --test`, `app.py --test`.

## Hosting (free tiers): Vercel + Turso + GitHub Actions
- **Turso** (Mumbai, database `mca`) holds the same SQLite schema `build_db.py` produces, uploaded with
  `turso db create mca --from-file roc.db` (WAL mode). After an upload, read every table once
  (`SELECT count(*)`): first reads of a fresh upload are slow (>10 s).
- **The daily refresh runs on a Mac** (`com.roc-scraper.fetch.plist`, launchd, 06:00 or on wake):
  `fetch.py` (today's states + any overdue >8 days), `sync_turso.py` (pushes only state files newer than
  Turso's copy, writing only new/changed/removed companies), then `build_db.py` (local copy).
  Needs `~/.datagov_key`, `~/.turso_mca_rw`, and `SSL_CERT_FILE` behind an HTTPS-inspecting proxy.
  data.gov.in refuses GitHub's servers, so `.github/workflows/refresh.yml` is manual-only, kept for a
  runner hosted in India.
- **Vercel** serves `index.html` and the search API (`api/index.py` -> `app.py`).
  Env: `TURSO_URL`, `TURSO_TOKEN` (read-only). Every push to `main` redeploys.

The typo-correction word list (`vocab`, `fix`) is only refreshed by a full rebuild + re-upload.
