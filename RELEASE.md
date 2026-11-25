# Release readiness: Company Search (RoC master data)

Status as of 2026-11-25. Works end to end on this Mac; **not yet releasable to other users**
(see blockers).

## What it is
- `fetch.py`: daily, pulls that weekday's states from the data.gov.in API into `data/<state>.csv`.
  All 37 states and UTs once a week, about 490k records/day. Run by launchd at 06:00 (`com.roc-scraper.fetch`).
- `build_db.py`: rebuilds `roc.db` (SQLite + full-text index) from `data/*.csv` after each fetch,
  carrying each record's "last updated" date over while its fields are unchanged.
- `app.py` + `index.html`: search UI (name / CIN, state filter, typo tolerance), stdlib only.

## Verified
- [x] State names: all 37 filter values confirmed against the API; together they cover all
      3,738,721 records.
- [x] Paging: sorted by CIN; Goa fetched fully twice with 15,897 distinct companies each time; deep offsets
      (765k) answer in 2-4 s. A state's file is only replaced if distinct CINs = API total.
- [x] Timeouts: 120 s per request (normal 2-5 s); 6 attempts with backoff up to ~15 min.
- [x] Scheduled job: run through launchd itself (`kickstart`), see "First scheduled run" below.
- [x] Last updated / last checked: self-check (`build_db.py --test`) and a real Goa re-fetch
      (15,897 rechecked, 0 changed, dates carried over).
- [x] Search: self-check (`app.py --test`); API smoke tests incl. typos, state filter, CIN,
      query-injection characters; broad queries capped at 1.5 s ("Too many matches").
- [x] Attribution footer (GODL-India requires crediting the source).

## Blockers before other people use it
- [ ] **Host it somewhere always-on.** Personal project, public site. Plan: Railway, one service from
      the `Dockerfile` with a volume at `/data` and `DATAGOV_KEY` set; `REFRESH_AT=06:00` runs the
      daily fetch in-process. First boot fetches all 37 states (~3 h) before search works.
- [ ] **HTTPS + abuse limits.** Railway terminates HTTPS. Still no request rate limit in front of
      Python's `http.server`; add one if traffic or scraping becomes a problem.
- [x] **Supported Python.** The host image is `python:3.12-slim` (this Mac's 3.9 is only used for the local copy).
- [x] **Version control.** Private GitHub repo (personal account). `data/`, `roc.db*`, `*.log` ignored;
      the API key lives in `~/.datagov_key` locally and a Railway variable when hosted, never in the repo.
- [ ] **Alert on failure.** A failed state is only written to `fetch.log`, and the old data stays live.
      Add an alert on non-zero exit, plus a staleness check (any state with `checked_at` older than 8 days).
- [ ] **Full coverage.** `fetch.py` now also fetches any state with no CSV yet, so the next run
      (Mac or host) fills in the 23 missing states (~3 h).
- [ ] **One fetcher only.** Once the host is verified, unload the Mac's launchd job so the same API
      key isn't used twice a day.

## Should fix
- [ ] Browser QA: layout on desktop/phone, dark mode, keyboard-only use (only API-tested so far).
- [ ] Rollback: `build_db.py` overwrites `roc.db`; keep the previous one as `roc.db.prev`.
- [ ] Ranking: active companies don't rank above struck-off ones; first-letter typos aren't corrected.
- [ ] Load test the hosted setup (concurrent users × typo-corrected searches).
- [ ] Disk headroom: ~1.1 GB CSVs + ~1.6 GB DB at full size, double the DB during a rebuild.

## Rollback triggers (once hosted)
- Searches slower than 1 s at p95, or any 5xx on `/api/search`.
- A daily run ending with failed states two days running.
- `roc.db` company count drops by more than 1% from the previous build.

## First scheduled run (launchd kickstart, 2026-11-25)
14:36 → 14:53 (17 min), exit code 0, no retries or timeouts.
West Bengal 294,366 (644 s) · Madhya Pradesh 88,551 · Bihar 82,243 · Goa 15,897 · Himachal Pradesh 12,647.
Rebuild 37 s → 3,197,316 companies (= previous 2,719,509 + 4 new states, exact).
New states: last updated = last checked = this run. Goa (seeded): last checked 25 Nov, last updated still 15 Nov (unchanged).
