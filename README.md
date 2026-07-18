<p align="center">
  <img src="assets/banner.png" alt="glassdoor-bff-scraper" width="100%" />
</p>

<h1 align="center">Glassdoor BFF Scraper</h1>

<p align="center">
  <em>Browser-free, block-resistant Glassdoor job scraping via the internal BFF API — with a hardened CLI and a FastAPI job-queue service.</em>
</p>

<p align="center">
  <a href="https://github.com/CJ7862/glassdoor-bff-scraper/actions/workflows/ci.yml"><img src="https://github.com/CJ7862/glassdoor-bff-scraper/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff" /></a>
  <img src="https://img.shields.io/badge/status-educational%20%7C%20unmaintained-lightgrey.svg" alt="Educational / unmaintained" />
</p>

Scrapes Glassdoor job listings by calling Glassdoor's **internal BFF JSON API**
directly, bypassing Cloudflare with TLS-fingerprint impersonation (`curl_cffi`)
instead of a browser. Ships as two things:

1. A hardened, tested Python package (`glassdoor_scraper`) with a first-class CLI.
2. A FastAPI service (`api`) that queues searches, stores results transiently,
   pushes signed webhooks, and exposes health/metrics — so downstream apps submit a
   search, poll or receive results, and manage their own long-term storage.

> **Educational project — read before using.** This repository is published for
> **educational and research purposes only**, as a reference for resilient
> API-based scraping techniques (TLS-fingerprint impersonation, async job queues,
> anti-detection session handling). It is provided **as-is and unmaintained**, with
> no support or guarantees.
>
> **Legal / ToS note:** scraping Glassdoor may violate its Terms of Service, and the
> pinned technique circumvents anti-bot protections. Running this against Glassdoor
> is your responsibility and risk. It keeps request rates conservative by default.
> This is **not legal advice**. Licensed under the MIT License (see `LICENSE`).

---

## How it works (and why not a browser)

A headless browser is the *most* detectable way to hit Glassdoor. In 2026 Glassdoor
sits behind Cloudflare (and DataDome), which run WebGL/canvas/behavioral checks that
headless Chromium fails, so the "Just a moment…" challenge hangs.

The method that works instead:

1. Glassdoor's React frontend fetches from an undocumented Backend-for-Frontend
   endpoint: `POST /job-search-next/bff/jobSearchResultsQuery`, returning clean JSON.
2. Cloudflare's edge check is largely JA3/JA4 TLS fingerprinting. `curl_cffi`
   reproduces a real Chrome TLS/HTTP2 handshake, so it passes where a headless
   browser gets challenged.
3. Flow: load the homepage on a **sticky** residential IP to pick up cookies, switch
   to CORS/API headers, then POST the BFF query; **rotate** IPs across pages.

### Important caveats (please read)

- **The pinned fingerprint is volatile.** Cloudflare whitelists specific TLS
  fingerprints. As of a third-party finding dated March 2026, `chrome136` passes
  while `chrome131`, `chrome142`, `safari`, and `firefox` were blocked. This *will*
  change. If you start getting `403`s, change the fingerprint (see the runbook). The
  scraper now also **auto-rotates** through a fallback chain on persistent blocks.
- **The BFF endpoint is undocumented** and its JSON shape can change without notice.
  The parser tolerates several known nesting variants; a sudden "ghost field" in the
  data-quality report is the early warning that the shape drifted.
- **DataImpulse is mid-tier on Cloudflare targets.** Expect more retries/blocks than
  a top-tier residential provider. Retry/backoff, best-effort bootstrap, a circuit
  breaker, and block-rate alerting exist to absorb and surface this.

---

## Project layout

```
glassdoor_scraper/          # Hardened core package
  config.py                 # pydantic-settings (env + overrides)
  session.py                # proxy URLs, session, anti-detection, safe_request
  parser.py                 # BFF JSON -> Job, cursor selection, shape tolerance
  models.py                 # Job dataclass + pay-period normalization
  scraper.py                # scrape_jobs: pagination, fingerprint fallback, breaker
  ratelimit.py              # global token-bucket rate limiter (sync + async)
  health.py                 # proxy-health + block-rate alerting
  reporting.py              # data-quality report (compute + render)
  export.py                 # atomic CSV/JSON writes + in-memory serialization
  runstate.py               # batch checkpoint/resume
  presenter.py              # rich CLI output with plain-text fallback
  cli.py                    # the CLI (python -m glassdoor_scraper ...)
api/                        # FastAPI service
  main.py                   # app factory, endpoints, middleware, lifespan
  db.py                     # thin SQLite repository (queue/results/seen/keys)
  worker.py                 # in-process asyncio worker pool
  schemas.py                # request/response validation
  auth.py                   # API-key auth, per-key rate limit, quotas
  security.py               # key hashing + HMAC webhook signing
  webhooks.py               # signed webhook delivery with retries
  metrics.py                # Prometheus metrics
  admin.py                  # API-key admin CLI (python -m api.admin ...)
glassdoor_jobs.py           # thin backward-compatible shim -> package CLI
tests/                      # pytest suite (all network mocked)
Dockerfile, docker-compose.yml, .github/workflows/ci.yml
```

---

## Install

```bash
cd Glassdoor-Scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # everything (runtime + dev/CI tools)
# or, for a slim runtime-only install:
# pip install -r requirements-runtime.txt
```

Configure proxies and options: copy `.env.example` to `.env` and fill it in, or
export the variables in your shell. The two most important are:

```bash
export DATAIMPULSE_USER="your_login"
export DATAIMPULSE_PASS="your_password"
```

The scraper uses the DataImpulse gateway `gw.dataimpulse.com`: **sticky** (port
`10000`, unique `sessid` per bootstrap) for session bootstrap, **rotating** (port
`823`) for collection. Country targeting maps to the DataImpulse `cr` parameter.

---

## CLI usage

Runs as `python -m glassdoor_scraper ...` (or the legacy `python glassdoor_jobs.py
...`, which still works).

Single search:

```bash
python -m glassdoor_scraper -k "data engineer" --city "San Francisco"
python -m glassdoor_scraper -k "data scientist" --city "New York" --sort date --pages 3
python -m glassdoor_scraper -k "devops" --city Berlin --site de --country de
```

With filters:

```bash
python -m glassdoor_scraper -k "backend engineer" --city "Bangalore" --site co.in --country in \
  --rating 3 --min-salary 1500000 --posted 1w --work-type remote --format both
```

Batch mode (multiple searches from CSV), resumable after an interruption:

```bash
python -m glassdoor_scraper --batch searches.csv --format both -o results
python -m glassdoor_scraper --batch searches.csv --resume   # skip finished rows
```

Known location id (skip city resolution):

```bash
python -m glassdoor_scraper -k "data engineer" --location-id 1147401 --location-name "New York"
```

Override the fingerprint, or force plain output for logs/pipes:

```bash
python -m glassdoor_scraper -k "data engineer" --city "New York" --impersonate chrome142
python -m glassdoor_scraper -k "data engineer" --city "New York" --no-color
```

### Options

| Flag | Description |
|------|-------------|
| `-k, --keyword` | Search keyword (required for single searches) |
| `-c, --city` | City name; auto-resolves to a location ID |
| `-l, --location-id` | Glassdoor location ID (skip city resolution) |
| `-b, --batch` | CSV file: `keyword, city, site, country, pages` |
| `--site` | Regional site: com, co.uk, ca, com.au, co.in, sg, de, fr, com.hk, co.nz |
| `--country` | 2-letter country code for the proxy geo (default: us) |
| `-p, --pages` | Result pages, 30 jobs each (default: 2) |
| `-s, --sort` | `relevant` or `date` |
| `-w, --work-type` | `remote` or `onsite` |
| `-e, --easy-apply` | Only Easy Apply jobs |
| `--rating` | Minimum company rating: any, 1, 2, 3, 4 |
| `--min-salary` / `--max-salary` | Salary range filters |
| `--posted` | any, 1d, 3d, 1w, 2w, 1m |
| `-o, --output` | Output filename (no extension) |
| `-f, --format` | `csv`, `json`, or `both` |
| `--delay-min` / `--delay-max` | Delay between requests (seconds) |
| `--proxy-user` / `--proxy-pass` | DataImpulse credentials (or use env vars) |
| `--impersonate` | Override the pinned curl_cffi fingerprint |
| `--resume` | Batch mode: skip rows already completed in a previous run |
| `--no-color` | Plain text output (also auto-selected when piped) |
| `--debug` | Verbose raw log stream |

### Output

Fields: `job_id, title, company, location, salary_min, salary_max, salary_currency,
pay_period, posted_date, easy_apply, company_rating, job_url, description_snippet`.
`pay_period` (ANNUAL/HOURLY/…) distinguishes an hourly `40–60` from an annual
`110000`. After each run the CLI prints a results summary and a **data-quality
report** (per-field population with GHOST/SPARSE flags). The pretty view uses `rich`
when attached to a terminal and falls back to plain aligned text when piped.

---

## API usage

Run the service:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
# or via docker compose (see Deployment)
```

Create an API key (shown once):

```bash
python -m api.admin create --name "acme-prod" --quota 1000 --rate 60 --concurrency 5
python -m api.admin list
python -m api.admin revoke --id <key-id>
```

### Browser test console

A self-contained test UI is served by the API itself at the root URL. Start the
service and open <http://localhost:8000/ui> (the root path `/` redirects there).
Paste an API key once (kept only in the browser's localStorage), fill the search
form, submit, and watch the job move through queued -> running -> done with a live
progress bar, then browse the paginated results table and download the CSV/JSON
export. The form's dropdowns (site, sort, rating, posted, work type) and the page
cap are loaded from `GET /v1/meta`, so they always match what the backend accepts.
It is a single static HTML file (`api/static/console.html`, no build step, no
external CDN) intended for manual testing and demos, not as an end-user product.

Submit a search (queued, runs asynchronously):

```bash
curl -sS -X POST http://localhost:8000/v1/searches \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "keyword": "data engineer",
        "city": "New York",
        "pages": 2,
        "sort": "date",
        "webhook_url": "https://your-app.example.com/hooks/glassdoor",
        "idempotency_key": "acme-2026-07-14-de-nyc"
      }'
# -> {"job_id":"...","status":"queued","idempotent":false,"created_at":"..."}
```

Poll status:

```bash
curl -sS http://localhost:8000/v1/searches/$JOB_ID -H "Authorization: Bearer $API_KEY"
# -> status: queued|running|done|failed, progress {pages_done, jobs_collected}, error, ...
```

Fetch results (paginated JSON, available until the TTL expires):

```bash
curl -sS "http://localhost:8000/v1/searches/$JOB_ID/results?page=1&page_size=50" \
  -H "Authorization: Bearer $API_KEY"
```

Bulk export the whole run as one file:

```bash
curl -sS "http://localhost:8000/v1/searches/$JOB_ID/export?format=csv" \
  -H "Authorization: Bearer $API_KEY" -o run.csv
curl -sS "http://localhost:8000/v1/searches/$JOB_ID/export?format=json" \
  -H "Authorization: Bearer $API_KEY" -o run.json
```

Health and metrics (no auth):

```bash
curl -sS http://localhost:8000/healthz
curl -sS http://localhost:8000/metrics      # Prometheus text exposition
```

### Endpoints

| Method & path | Purpose |
|---|---|
| `POST /v1/searches` | Submit a search; returns `job_id`. Idempotent by `idempotency_key` or identical body within a short window. |
| `GET /v1/searches/{job_id}` | Status + progress + error info. |
| `GET /v1/searches/{job_id}/results` | Paginated results JSON (until TTL). |
| `GET /v1/searches/{job_id}/export?format=csv\|json` | Whole run as one downloadable file. |
| `GET /healthz` | Liveness + queue/proxy snapshot. |
| `GET /metrics` | Prometheus metrics (success/block rates, queue depth, proxy failures). |
| `GET /v1/meta` | Form option lists (sites, sorts, ratings, posted, work types), page cap, and job fields. No auth. |
| `GET /ui` | Browser test console (static HTML). `GET /` redirects here. |

### Webhooks

If you pass `webhook_url`, the completed payload (`job_id`, `status`, `stats`,
`quality`, `results`) is POSTed there when the job finishes, with a couple of retries.
Each request carries `X-Glassdoor-Timestamp` and `X-Glassdoor-Signature`
(`HMAC-SHA256` over `"{timestamp}.{body}"` using `GLASSDOOR_WEBHOOK_SECRET`). Verify
on your side:

```python
import hmac, hashlib
def verify(secret, timestamp, body_bytes, signature):
    msg = timestamp.encode() + b"." + body_bytes
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Storage model

- **Queue + transient results + seen-jobs index + API keys** live in one SQLite file
  (WAL mode). The data layer is a thin repository (`api/db.py`) so it can be swapped
  for Postgres/Supabase later without touching the endpoints or worker.
- **Results expire** after `GLASSDOOR_RESULTS_TTL_HOURS` (default 48h); a background
  task purges them. **You own long-term storage** — fetch within the window and
  persist on your side.
- A lightweight **`seen_jobs`** index (listing id, first/last seen, search context)
  persists past the TTL so repeat listings are flagged across runs without keeping
  full payloads.
- Jobs **survive restarts** (they are rows). A failed job is retried up to
  `GLASSDOOR_JOB_MAX_ATTEMPTS`, then moved to a terminal `failed` (dead-letter) state
  with the error captured — never silently lost.

### Auth, quotas, and abuse guards

- API keys are hashed at rest (SHA-256 of a high-entropy random token).
- Per-key **daily search quota**, **requests/minute** rate limit, and
  **max concurrent jobs**.
- Hard cap on `pages` per request and a **payload size limit**, both enforced at the
  schema/boundary. Every input is schema-validated before it reaches the queue.

---

## Deployment

```bash
cp .env.example .env      # fill in proxy creds + GLASSDOOR_WEBHOOK_SECRET, etc.
docker compose up --build
```

- `Dockerfile`: slim Python, non-root user, `/data` volume for the SQLite file,
  container `HEALTHCHECK` against `/healthz`.
- `docker-compose.yml`: single service, named volume `glassdoor_data`, a 40s
  `stop_grace_period` so in-flight jobs checkpoint/requeue on SIGTERM.
- Graceful shutdown: on SIGTERM the service stops taking new jobs, lets in-flight
  pages finish or checkpoints them, and requeues anything unfinished.

---

## Testing, linting, CI

```bash
python -m pytest            # all tests; no live network (everything is mocked)
ruff check .                # lint
python -m mypy glassdoor_scraper api   # type-check
pip-audit -r requirements.txt          # dependency vulnerability audit
```

`.github/workflows/ci.yml` runs lint, type-check, tests, and `pip-audit` on push/PR.

---

## Ops runbook

**Symptom: a wave of 403s / "challenge page detected" / block-rate alert fires.**
The pinned TLS fingerprint most likely stopped passing Cloudflare.
1. The scraper already auto-rotates through `GLASSDOOR_IMPERSONATE_FALLBACKS`; check
   logs for `Fingerprint fallback succeeded with '<target>'` and, if one consistently
   passes, promote it to `GLASSDOOR_IMPERSONATE`.
2. List targets your installed `curl_cffi` supports:
   `python -c "from curl_cffi.requests.impersonate import BrowserTypeLiteral; import typing; print(typing.get_args(BrowserTypeLiteral))"`.
3. Confirm the proxy itself works:
   `curl -x "http://USER__cr.us:PASS@gw.dataimpulse.com:823" https://api.ipify.org`.
4. The **circuit breaker** stops a run after
   `GLASSDOOR_CIRCUIT_BREAKER_THRESHOLD` consecutive blocks to conserve proxy
   bandwidth — raise/lower it as needed.

**Symptom: ghost fields in the quality report / schema-drift alert.**
The BFF JSON shape changed. Update the extraction in `glassdoor_scraper/parser.py`
against a fresh response and add/adjust a fixture in `tests/fixtures/`.

**Symptom: results 410 Gone.** They passed their TTL; re-submit the search.
Consumers should persist results on their side within the TTL window.

**Symptom: jobs stuck in `queued`.** Check `GET /healthz` for `workers` and queue
depth; check logs for worker errors. Restart is safe — running jobs are requeued.

**Symptom: proxy pool degrading.** Watch `glassdoor_proxy_requests_total{outcome=…}`,
`glassdoor_rolling_block_rate`, and `glassdoor_proxy_bytes_total` in `/metrics`, plus
the `proxy_health` block in `/healthz`.
