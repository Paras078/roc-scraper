# Company Search (RoC master data)

Search Indian companies by name or CIN, over the Ministry of Corporate Affairs
[RoC company master data](https://www.data.gov.in/resource/registrars-companies-roc-wise-company-master-data)
from data.gov.in (Government Open Data License – India). Python stdlib only.

- `fetch.py` pulls each state from the data.gov.in API once a week (that weekday's states, plus any never fetched).
- `build_db.py` rebuilds `roc.db` (SQLite full-text index), keeping each record's "last updated" date while it is unchanged.
- `app.py` + `index.html` is the search UI: typo tolerant, with a state filter.

## Run locally
    echo YOUR_KEY > ~/.datagov_key && chmod 600 ~/.datagov_key
    python3 fetch.py goa && python3 build_db.py && python3 app.py   # http://127.0.0.1:8000
Self-checks: `python3 fetch.py --test`, `build_db.py --test`, `app.py --test`.

## Hosting (Railway)
The `Dockerfile` runs `app.py`, which also does the daily refresh at 06:00 IST (`REFRESH_AT`).
Attach a volume at `/data` and set the `DATAGOV_KEY` variable. The first boot fetches all states (~3 h)
before search returns results. `com.roc-scraper.fetch.plist` is the launchd job for running it on a Mac instead.
