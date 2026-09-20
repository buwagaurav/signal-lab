# SignalLab

SignalLab is an explainable indicator validator and paper-trading journal. It is intentionally read-only: no broker credentials, live orders, or external market-data calls are included.

The frontend (`index.html` / `app.js`) is a dependency-free static page that now calls the FastAPI backend in `backend/` directly — the signal validator, trade journal, and market-data badge all read real data from the API instead of sample formulas. No signup screen is shown: the page transparently registers/logs in one fixed local demo account on first load.

## Run locally

Start the backend first (see below), then serve the frontend from the repo root with any static server:

```bash
python3 -m http.server 8080
```

Then visit `http://localhost:8080`. The default `CORS_ORIGINS` in `backend/.env.example` already allows this origin.

If the backend isn't reachable, the UI shows a "backend offline" badge and the validator/journal panels report the error instead of falling back to fake numbers.

## Product architecture

```text
Market-data adapters → normalized OHLCV store → deterministic indicator engine
       → signal/rule validator → risk gate → paper-trading ledger
       → evidence-linked explanation service → alerts/review UI
```

The UI currently uses demo data and front-end simulation. A production build should keep indicator calculation and P&L in a trusted backend service, use decimal arithmetic, version every strategy, and store the exact data snapshot used for every backtest.

## Security and guardrails

- Keep live trading disabled until a separate, audited execution service exists.
- Use read-only broker scopes for imports; never store raw broker passwords or API tokens.
- Put secrets in a managed secrets vault, not browser storage or source code.
- Require stop-loss, max-risk, daily-loss, stale-data, and liquidity checks before alerts.
- Add human confirmation, idempotency keys, rate limits, audit logs, and reconciliation before any order capability.
- Treat AI as an evidence-linked explanation layer; it must not calculate prices, invent data, or bypass risk gates.
- Show backtest assumptions, costs, slippage, sample size, drawdown, and out-of-sample results.

## Backend foundation

The `backend/` directory now contains a FastAPI foundation with JWT authentication, Argon2 password hashing, SQLAlchemy models, PostgreSQL Docker Compose setup, SQLite local development, broker CSV parsing, deterministic EMA/RSI/ATR/volume logic, a backtest engine, idempotent paper-alert evaluation, paper-trade endpoints, and a fail-closed licensed NSE data adapter.

Run locally:

```bash
cd backend
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The backtest route runs only against stored candles from the configured licensed provider. If the provider is absent or data is missing, the API fails closed — which is the normal state for local development, since no vendor is configured. To exercise the validator UI locally, seed clearly-labeled synthetic candles instead:

```bash
python -m app.seed_demo_data
```

This inserts synthetic (not real) OHLCV history for the four symbols the demo UI offers, tagged `source="seed_demo"`, so `/backtests` has real stored data to run the deterministic engine against.

### Connecting the real licensed provider (ICICI Direct Breeze)

`backend/app/providers/licensed_nse.py` is a concrete adapter to the [Breeze API](https://pypi.org/project/breeze-connect/), ICICI Direct's official trading/data API. It requires an actual ICICI Direct trading account with a registered Breeze app — this is not something the app can obtain on its own.

1. Register an app at ICICI Direct's Breeze developer portal to get an **App Key** and **Secret Key**.
2. Get a session token by visiting `https://api.icicidirect.com/apiuser/login?api_key=<url-encoded App Key>`, logging in, and copying the `API_Session` value from the redirect URL. **This token expires roughly every 24 hours / at midnight** — it is not a "set once" secret like the other two, and must be refreshed (and the backend restarted, since settings are cached at startup) whenever it lapses.
3. Set all three in `backend/.env`:
   ```
   BREEZE_API_KEY=...
   BREEZE_API_SECRET=...
   BREEZE_SESSION_TOKEN=...
   ```
4. Pull real candles into storage:
   ```bash
   curl -X POST "http://127.0.0.1:8000/data/nse/sync?symbol=RELIANCE&timeframe=1d&start=2023-01-01T00:00:00&end=2026-09-20T00:00:00" \
     -H "Authorization: Bearer $TOKEN"
   ```
   Once real candles exist for a symbol + timeframe, `/backtests` uses them automatically ahead of any leftover seed data.

Notes and open items:
- The four demo symbols map to Breeze's internal `stock_code` in `SYMBOL_TO_STOCK_CODE` in that file. All four are confirmed working against a live account: `RELIANCE`→`RELIND`, `INFY`→`INFTEC`, `HDFCBANK`→`HDFBAN` (verified against ICICI's own public security master file), and `NIFTY 50`→`NIFTY` (confirmed by a live sync returning index-level close prices, ~23,000-24,000, which no individual stock trades at).
- `breeze-connect` is a synchronous SDK and does network I/O as a side effect of `import breeze_connect` (it downloads a ~3MB security master zip and creates a `logs/` directory). The adapter imports it lazily, inside the method that needs it, specifically so the backend's own startup never depends on that succeeding.
- An automated way to refresh the session token (or a vendor with longer-lived credentials) is still open, as is an independent security review — see below for what else has since been added.

## Money math, alert delivery, audit logs, rate limits

- **Decimal, not float, for money.** `Candle`/`Trade` columns and the `CandleInput`/`PaperTradeCreate` API schemas use `Decimal`, and `services/backtest.py`'s entry/stop/target/exit/P&L math is real `Decimal` arithmetic — summing many trades' P&L can't accumulate binary floating-point rounding error. Indicator computation (EMA/RSI/ATR) stays on pandas' float64 `.ewm()`/`.rolling()` deliberately: those are technical/statistical signals, not settlement money, and pandas doesn't have a practical native Decimal path for them anyway (it silently coerces object-dtype Decimal columns through float64 internally regardless). `Decimal`-typed API response fields serialize as JSON **strings** (e.g. `"10.00000000"`), not numbers — `app.js` wraps them in `Number()` before display.
- **Alert delivery** (`app/scheduler.py`, `app/services/alerts.py`): an in-process APScheduler job, off by default (`ALERTS_SCHEDULER_ENABLED=false`), polls every `Strategy` with `enabled: true` on `ALERT_POLL_SECONDS`, pulls fresh candles from the licensed provider, evaluates the signal, and creates at most one idempotent `AlertEvent` per strategy-version-candle. The evaluation logic is shared between this poller and the `/alerts/evaluate/{id}` HTTP route — one place creates events, not two. Delivery goes through a `NotificationChannel` interface; the only implementation so far is `LoggingNotificationChannel`, which logs structurally rather than emailing/messaging anyone, since no delivery credentials (SMTP, Slack webhook, ...) have been configured or asked for. Swap in a real channel behind the same `deliver(event) -> bool` interface when one exists. **Turning this on calls the licensed provider unattended on a timer** — be mindful of Breeze's 100/min, 5000/day limits before enabling it.
- **Audit log** (`app/audit.py`, `AuditLog` table): middleware records method/path/status/user_id/client_ip for every request except `/health` and the docs routes. Resolves the user from the bearer token itself (best-effort; an invalid token just logs as anonymous, since the route already separately rejects it). A logging failure never fails the request it's observing.
- **Rate limiting** (`app/rate_limit.py`): in-memory sliding-window limiter, 10/min on `/auth/login` and `/auth/register`, 120/min everywhere else, per client IP. In-process only — correct for one uvicorn worker, not for multiple instances behind a load balancer (each keeps independent counts). A shared store (Redis) is the upgrade path if that stops being true.
- **Not done, deliberately deferred**: Alembic migrations (still `Base.metadata.create_all`), a durable task queue (Celery+Redis, if the in-process poller's single-instance/no-restart-durability limits become a problem), and a secrets vault (there's no deployment target yet to build one against — `.env` is fine for a single local instance, but is not where `JWT_SECRET`/Breeze credentials should live once this runs anywhere else). An independent security review is exactly that — independent — and isn't something the same tool that wrote the code can perform for you.

## Deploying

Breeze requires a **static IP** registered with ICICI (changeable only once/week) for live data to keep working, which rules out most PaaS platforms (Railway, Render, Heroku, Vercel) — they don't give you a fixed outbound IP. This targets a small VM instead, using the `docker-compose.yml` + `Caddyfile` already in the repo: Caddy serves the static frontend and reverse-proxies `/api/*` to the backend on the same origin, so there's no cross-origin config to fight in production, and the API's raw port is never exposed publicly.

### 1. Provision the VM

Any provider with a static IP works; DigitalOcean's Docker marketplace image is the least setup:

1. Create a DigitalOcean account (or use one you have).
2. **Create → Droplets → Marketplace tab → "Docker"** (ships with Docker + Compose preinstalled). Cheapest plan (~$6/mo) is enough to start.
3. Note the droplet's public IP once it's up — **this is the IP you'll register with Breeze** as your static IP.
4. `ssh root@<droplet-ip>`

### 2. Deploy the app

This repo has no git remote set up yet, so copy it to the server directly rather than `git clone`ing it (run this from your laptop, not the VM):

```bash
rsync -avz --exclude backend/.venv --exclude backend/.env --exclude backend/signal_lab.db --exclude backend/**/__pycache__ \
  /Users/gbuwa/Documents/Codex/2026-09-20/signalLab/ root@<droplet-ip>:signallab/
```

(If you later push this to GitHub, `git clone <url> signallab` on the VM works the same way and is easier to keep updated.)

Then, back on the VM:
```bash
cd signallab
cp backend/.env.production.example backend/.env.production
```

Edit `backend/.env.production`:
- `JWT_SECRET` — generate a real one: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Never leave the placeholder — it's a well-known default, and anyone who recognizes it can forge valid login tokens.
- `DATABASE_URL` — replace the password with a strong one.
- `BREEZE_API_KEY` / `BREEZE_API_SECRET` / `BREEZE_SESSION_TOKEN` — same values as your local `backend/.env`, once this VM's IP is registered on Breeze's side (step 4 below).

Then edit `docker-compose.yml`'s `db.environment.POSTGRES_PASSWORD` to the **same** password you just put in `DATABASE_URL` — those are two separate files with no automatic link between them.

```bash
docker compose up -d --build
curl http://localhost/health
```

Should return `{"status":"ok",...}`. Visit `http://<droplet-ip>` in a browser — same UI as local, now running on the server.

### 3. Seed or sync data

The fresh Postgres database starts empty. Either:
```bash
docker compose exec api python -m app.seed_demo_data
```
for synthetic data, or sync real candles the same way as local (see above) once Breeze is configured — except the API's port 8000 is no longer published to the host (`docker-compose.yml`'s `api` service uses `expose`, not `ports`, so only Caddy can reach it). Go through Caddy's proxy instead: `http://<droplet-ip>/api/data/nse/sync?...` — same path, `/api` prefix, port 80 instead of 8000.

### 4. Register the VM's IP with Breeze

Same [portal steps as local](#connecting-the-real-licensed-provider-icici-direct-breeze), except the **static IP field is now the droplet's public IP**, not your laptop's. If your Breeze app was originally registered with your home IP, update it — remember it's rate-limited to once/week.

### 5. Add HTTPS once you have a domain

Point a domain's A record at the droplet's IP, then in `Caddyfile` replace:
```
:80 {
```
with:
```
yourdomain.example {
```
and `docker compose restart caddy`. Caddy requests and renews a real Let's Encrypt certificate automatically — no other config changes. Until you do this, traffic (including login credentials) travels unencrypted over plain HTTP — fine for initial testing, not for anything real.

### What I could and couldn't verify

Docker isn't available in the environment this was built in, so `docker compose up` itself was never run end-to-end here. What *was* verified: `docker-compose.yml`'s YAML parses correctly, every service name / port / env-var reference between `docker-compose.yml`, `Caddyfile`, and `.env.production.example` was cross-checked by hand, and a real mount bug was caught and fixed in review (mounting the whole repo into Caddy's static root would have served `backend/.env.production` to anyone who requested that path — fixed to mount only the three actual frontend files). The `curl http://localhost/health` check in step 2 above is where this gets its first real runtime test — worth doing that before anything else once the VM is up.
