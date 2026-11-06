# Stock Credit Spread System

Scans liquid NSE stock options daily, finds defined-risk credit spreads (bull put, bear call)
when implied volatility is unusually rich, and paper-trades them with deliberately pessimistic fills.
Live order placement is built last and stays off until the spec's go-live criteria are met.

**Status:** milestones 1-4 complete.

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Skeleton: config, SQLite schema, logging, `doctor` | done |
| 2 | Broker and data layer (Kite Connect, MockBroker) | done |
| 3 | Analytics (Black-76, IV solver, HV, ATR) | done |
| 4 | Levels and events | done |
| 5 | Scanner (F1–F9, EV ranking) | |
| 6 | Paper engine, monitor, scheduler | |
| 7 | Reporting and Telegram | |
| 8 | Live adapter (only when the owner asks) | |

## Setup

```bash
cd credit_spreads
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp .env.example .env        # fill in secrets later; never commit .env
python -m app migrate       # creates data/trading.db
python -m app doctor        # prints config and database status
python -m pytest            # run the tests
```

## Safety

- `mode: paper` is the default.
- Any code that sends a real order calls `app.safety.require_live()` first. That function raises
  unless `mode: live` is set in `config.yaml` **and** `LIVE_TRADING_CONFIRMED=yes-real-money` is set in
  the environment. In paper mode, live order paths are unreachable.
- `live.max_ops` is capped at 2 orders per second by validation, and `live.require_approval` must be true.

## Configuration

Every threshold is in `config.yaml` and is validated on load. Unknown or misspelt keys are an error,
not a silent default. `costs.exchange_txn_pct` is left blank on purpose: the cost model will refuse to
price trades until the owner fills it in from the current NSE circular.

## Database

All state is in one SQLite file, `data/trading.db`. The schema is owned by forward-only SQL
migrations in `app/data/migrations/`; the ORM models in `app/data/models.py` mirror them, and a test
fails if the two drift apart. Timestamps are stored as naive IST.

## Layout

```
app/
  config.py  env.py  safety.py  clock.py  fmt.py  logging_setup.py  doctor.py  cli.py
  broker/  data/  analytics/  strategy/  risk/  execution/  monitor/  reporting/
tests/
config.yaml  levels_manual.csv  events_override.csv  .env.example
```
