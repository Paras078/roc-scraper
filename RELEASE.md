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
- [x] **Hosting** (free tiers): Vercel (UI + API, Mumbai) + Turso `mca` (Mumbai). Live at
      https://roc-scraper.vercel.app with all 37 states (3,738,718 companies, 26 Nov).
- [x] **Daily refresh reaches the live site.** Mac launchd: fetch -> sync_turso -> build_db. Tested
      end to end in a bare environment (Lakshadweep, 26 Nov).
- [x] **Missed days catch up.** Any state not fetched for >8 days is fetched on the next run.
- [x] **Supported Python** on Vercel (3.12). The Mac job still uses the system 3.9.
- [x] **Version control.** Public GitHub repo (personal account); no secrets in history (checked).
- [ ] **Single point of failure: the Mac.** Runs only while it's on (06:00, or on wake). data.gov.in
      refuses GitHub's servers, so moving off the Mac needs an India-hosted machine (e.g. Oracle free, Mumbai).
- [ ] **Alert on failure / staleness.** A failed run only shows in `fetch.log`. Add a check that warns when
      any state's `checked.at` is older than 8 days.
- [ ] **Cold reads.** First search in an untouched part of the data can take 1-5 s (was timing out at 10 s;
      now 25 s allowed, and the whole DB is read once after each upload).
- [ ] **Abuse limits.** No rate limit on the public API; Turso free caps reads at 500M/month, no overage billing.

## Should fix
- [ ] Browser QA: layout on desktop/phone, dark mode, keyboard-only use (only API-tested so far).
- [ ] Rollback: a bad sync can't be undone; Turso point-in-time restore on the free plan is limited.
- [ ] Ranking: active companies don't rank above struck-off ones; first-letter typos aren't corrected.
- [ ] Load test the hosted setup (concurrent users × typo-corrected searches).
- [ ] Storage: ~1.6 GB of Turso's free 5 GB at full size.

## Rollback triggers (once hosted)
- Searches slower than 1 s at p95, or any 5xx on `/api/search`.
- A daily run ending with failed states two days running.
- Company count (meta) drops by more than 1% after a sync.
- Typo-fix word list (`vocab`, `fix`) is only refreshed by a full rebuild + re-upload; do one monthly.

## First scheduled run (launchd kickstart, 2026-11-25)
14:36 → 14:53 (17 min), exit code 0, no retries or timeouts.
West Bengal 294,366 (644 s) · Madhya Pradesh 88,551 · Bihar 82,243 · Goa 15,897 · Himachal Pradesh 12,647.
Rebuild 37 s → 3,197,316 companies (= previous 2,719,509 + 4 new states, exact).
New states: last updated = last checked = this run. Goa (seeded): last checked 25 Nov, last updated still 15 Nov (unchanged).
