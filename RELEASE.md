# Release readiness: Company Search (RoC master data)

Status as of 2026-11-25. Works end to end on this Mac; **not yet releasable to other users**
(see blockers).

## What it is
- `fetch.py`: daily, pulls that weekday's states from the data.gov.in API into `data/<state>.csv`.
  All 37 states and UTs once a week, about 490k records/day. Run by launchd at 06:00 (`com.roc-scraper.fetch`).
- `build_db.py`: rebuilds `roc.db` (SQLite + full-text index) from `data/*.csv` after each fetch,
  carrying each record's "last updated" date over while its fields are unchanged.
- `sync_turso.py`: hosted copy; pushes only changed companies into Turso.
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
- [ ] **Hosting** (all free tiers): Vercel (UI + search API) + Turso, Mumbai (database) +
      GitHub Actions (daily fetch, writes only changes). See README. Verify on the live URL.
- [x] **Supported Python.** Vercel and GitHub Actions run Python 3.12.
- [x] **Version control.** Private GitHub repo (personal account). Data, DB, logs ignored; keys only
      in `~/.datagov_key` locally and as GitHub/Vercel secrets.
- [x] **Alert on failure.** A failed daily run (any state) turns the GitHub Action red, which emails you.
- [ ] **data.gov.in from GitHub's servers.** Unverified until the first Actions run.
- [ ] **Full coverage.** 14 of 37 states uploaded. The Mac's 06:00 run on 26 Nov fetches the
      other 23; then rebuild + re-upload once.
- [ ] **One fetcher only.** After that, unload the Mac's launchd job.
- [ ] **Cold starts.** First searches after Turso sits idle took 6-31 s (warm: 60-220 ms). Measure
      on the live site; add a warm-up ping if it persists.
- [ ] **Abuse limits.** No rate limit on the public API; Turso's free plan caps reads at 500M/month
      (a search reads ~100-1,500 rows) and does not bill overages.

## Should fix
- [ ] Browser QA: layout on desktop/phone, dark mode, keyboard-only use (only API-tested so far).
- [ ] Rollback: a bad sync can't be undone; Turso point-in-time restore on the free plan is limited.
- [ ] Ranking: active companies don't rank above struck-off ones; first-letter typos aren't corrected.
- [ ] Load test the hosted setup (concurrent users × typo-corrected searches).
- [ ] Storage: ~1.6 GB of Turso's free 5 GB at full size.
- [ ] GitHub Actions minutes (private repo: 2,000 free/month): ~30-45 min/day, so ~1,000-1,400.

## Rollback triggers (once hosted)
- Searches slower than 1 s at p95, or any 5xx on `/api/search`.
- A daily run ending with failed states two days running.
- Company count (meta) drops by more than 1% after a sync.

## First scheduled run (launchd kickstart, 2026-11-25)
14:36 → 14:53 (17 min), exit code 0, no retries or timeouts.
West Bengal 294,366 (644 s) · Madhya Pradesh 88,551 · Bihar 82,243 · Goa 15,897 · Himachal Pradesh 12,647.
Rebuild 37 s → 3,197,316 companies (= previous 2,719,509 + 4 new states, exact).
New states: last updated = last checked = this run. Goa (seeded): last checked 25 Nov, last updated still 15 Nov (unchanged).
