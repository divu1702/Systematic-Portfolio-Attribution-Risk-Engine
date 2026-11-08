# Stock Credit Spread System

Scans liquid NSE stock options daily, finds defined-risk credit spreads (bull put, bear call)
when implied volatility is unusually rich, and paper-trades them with deliberately pessimistic fills.
Live order placement (milestone 8) is not built and stays off until the go-live criteria are met.

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Skeleton: config, SQLite schema, logging, `doctor` | done |
| 2 | Broker and data layer (Kite Connect, MockBroker) | done |
| 3 | Analytics (Black-76, IV solver, HV, ATR) | done |
| 4 | Levels and events | done |
| 5 | Scanner (F1–F9, EV ranking) | done |
| 6 | Paper engine, monitor, scheduler | done |
| 7 | Reporting and Telegram alerts | done |
| 8 | Live adapter | not started (only when the owner asks, after go-live) |

## Setup (on the laptop that will run it)

```bash
git clone -b claude/document-review-setup-iy6jnq https://github.com/divu1702/Systematic-Portfolio-Attribution-Risk-Engine
cd Systematic-Portfolio-Attribution-Risk-Engine/credit_spreads
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate ; Python 3.11+
pip install -r requirements.txt
cp .env.example .env          # type your Kite key/secret and Telegram token here; never commit .env
python -m app migrate         # creates data/trading.db
python -m app doctor          # config, secrets (set/missing only), database status
python -m pytest              # ~160 offline tests
```

## Daily use

| When | What |
|------|------|
| Once | `python -m app run` and leave it running (laptop plugged in, sleep off, on wifi). It schedules everything below. |
| 09:00 | Telegram sends the Kite login link if there is no session. Log in, copy `request_token` from the address bar, send `/token <it>` to the bot (or `python -m app login` on the laptop). |
| 09:20 | Gap check on open spreads. |
| every 5 min | Marks open spreads at natural prices and applies the exit rules. 15:15 is the thesis-stop check. |
| 15:00 | Scan, then paper entries for the top candidates that pass sizing and portfolio limits. |
| 15:35 | Daily candles, levels, turnover, events, equity and drawdown. |
| Friday 16:00 | Weekly HTML report in `reports/`. |

If the laptop sleeps, the next monitor run replays the missed 5-minute marks from Kite candles
(pessimistic prices, flagged `replayed`), so stops and targets are not silently skipped.

Telegram commands: `/status`, `/kill [reason]`, `/resume`, `/resume_drawdown`, `/login`, `/token`,
`/report`, `/pending`, `/approve N`, `/reject N`, `/help`. Only your `TELEGRAM_CHAT_ID` is obeyed.

Other commands: `scan`, `chain SYMBOL`, `monitor`, `eod`, `status`, `kill`, `resume [--drawdown]`,
`report`, `record SYMBOL...`. Add `--mock` to run any of them on the synthetic market.

## Offline demos (no Kite needed)

```bash
python -m app chain STK01 --mock                       # option chain with IVs and deltas
python -m app scan --mock --warmup 135 --why           # ranked candidates + rejection funnel
python -m app sim --warmup 130 --days 60 --outages 0.15  # full paper run, then:
python -m app report --db data/trading_mock.db
streamlit run app/reporting/dashboard.py -- --db data/trading_mock.db   # optional
```

The synthetic market (made-up tickers STK01..) tests the machinery. Its results say nothing about
whether the strategy has an edge.

## Backtest on real data (US stock options)

Indian historical option quotes are not freely available to automated clients (NSE's site runs a bot
check, and Kite has no history for expired contracts), so the real-data backtest uses DoltHub's free
US data: end-of-day bid/ask for ~25 strikes x 3 expiries per stock per day, daily bars, splits and
earnings dates. The replay drives the **same** instrument refresh, scanner, entries, monitor and
end-of-day jobs as live trading, one trading day at a time, with no data after the simulated day.

```bash
python -m app backtest load --start 2024-01-02 --end 2026-10-30     # ~1 hour, resumable
python -m app backtest run --warmup 130 --report                     # spec thresholds, owner rules
python -m app backtest run --set iv.min_iv_hv_ratio=1.15 --db data/bt_f3.db   # a variant
```

Differences from live: US market (dollars, 100-share contracts, per-contract fees of $0.70, weekly
expiries), stops checked once a day at the close instead of every 5 minutes, no 09:20 gap check,
order-book size unknown (depth never binds), and legs that drop out of the day's data sample are
priced with Black-76 at their last observed IV (counted in the summary). Universe and sectors:
`app/backtest/us_universe.csv`.

## First run on real data: checklist

These could not be verified offline (the build machine has no access to Zerodha or NSE):

1. `python -m app login`, then `python -m app chain <a liquid F&O stock>`: lot size, strike step,
   futures price and IVs should look like Kite's own option chain.
2. `python -m app scan --why`: check `logs/app.log` for NSE warnings. The system asks NSE for events,
   sectors and holidays with an honest client identity and does not try to get past NSE's bot check.
   If NSE refuses, F2 fails closed (no trades). Then download the Board Meetings and Corporate
   Actions CSVs from nseindia.com in your browser and drop them into `imports/events/` (weekly is
   enough: each file counts as fresh for 2 days after you save it), or use a licensed data feed.
3. Expect no trades for the first few weeks. F5 needs IV snapshots from earlier days (fails closed
   until then), and F4 uses the HV-percentile proxy until 120 trading days of IV history exist.
4. `python -m app record <SYMBOL> <SYMBOL>` saves real responses under `fixtures/` for offline tests.
5. Telegram: create a bot with @BotFather, put its token and your chat ID in `.env`, restart `run`.

## Safety

- `mode: paper` is the default. The order router builds the paper executor in paper mode; in live
  mode it first calls `require_live()` (needs `mode: live` **and** `LIVE_TRADING_CONFIRMED=yes-real-money`)
  and then stops, because the live adapter is not built.
- Exits close the short leg first and never sell the long while the short is open.
- Kill switches block new entries (exits keep running): `/kill`, the daily loss stop, the drawdown pause
  (manual reset), stale quotes, more than 3 order rejections a day, and the 09:15–09:20 window.
- Event data fails closed: stale data, no results history, an event before expiry, or (configurable)
  results estimated to fall in the window all reject the stock.

## Risk and universe (owner's rules)

- Size by the loss at the hard stop: a stop at 2x credit loses about 1x credit (x1.25 allowance for
  fills past the trigger). 2% of equity per trade, 8% across open spreads, on that basis.
- Gap guard: full max loss (what a gap straight through the stop would cost) is capped at 5% of equity
  per trade and 20% in total.
- Every F&O stock is scanned. A spread qualifies on liquidity if both legs have a two-sided quote with
  bid-ask within 5% of mid and enough bids and asks in the 5-level book to fill the planned size.

## How EV is estimated

For each candidate spread the scanner simulates 2,000 price paths to the time stop and applies the
exit rules along each path (hard stop, thesis stop at the daily close, profit target), closing the
rest at the time stop. EV is the average P&L after costs and the cost of crossing the bid-ask to
close; p_model is the share of paths that make money. A spread needs EV > 0 and EV >= 3% of max loss.
IV is held at its entry level in the simulation, so any fall in IV after entry is upside it ignores.

## Configuration

Every threshold is in `config.yaml`, validated on load; unknown keys are an error. Keys marked `# +` are
numbers from the spec's prose (and a few operational settings) exposed so nothing is hard-coded.
Threshold changes go in batches after every 30 closed trades, recorded in `CHANGELOG.md`.

## Layout

```
app/
  config.py env.py safety.py clock.py context.py cli.py doctor.py jobs.py scheduler.py sim.py market.py views.py
  broker/     base.py kite.py login.py mock.py synthetic.py
  data/       db.py models.py migrations/ instruments.py nse.py events.py daily.py levels_store.py refdata.py ...
  analytics/  black76.py vol.py levels.py
  strategy/   scanner.py spreads.py costs.py ev.py
  risk/       sizing.py limits.py equity.py
  execution/  paper.py entries.py router.py
  monitor/    marks.py exits.py replay.py
  reporting/  metrics.py charts.py weekly.py telegram_bot.py approvals.py notifier.py dashboard.py templates/
tests/
config.yaml  levels_manual.csv  events_override.csv  sectors_manual.csv  holidays_manual.csv  .env.example
```
