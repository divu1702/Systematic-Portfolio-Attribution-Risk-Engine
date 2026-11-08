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
python -m pytest              # ~230 offline tests
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
| every 15 min, 09:32-15:17 | Bid/ask snapshots of the strikes the strategy trades, saved for backtests (see below). |
| 16:10 | Kite history archiver: saves 1-minute candles of listed options and futures before Kite drops them (see below). |
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

### Your own NSE history (recorded while paper trading)

While `python -m app run` is going, every 15:00 scan saves the quotes it evaluated (real bid/ask,
top-of-book and 5-level sizes, OI, volume, lot size and tick, futures prices) and the end-of-day job
saves bars, sectors and the date each corporate event was first seen, into `data/market_nse.db`
(`ops.record_market`, `paths.market_store`; roughly 1-2 MB a day). After a few months:

```bash
python -m app backtest run --market nse --warmup 30 --report
```

replays that history through the same code with your rupee capital and Zerodha costs. Unlike the US
data, it carries real order-book depth, and events are visible exactly from the day they were first seen.

### Kite history archiver (Indian option history before it disappears)

Kite serves candles only for contracts that are still listed: when a monthly series expires its
option history is gone, and expired futures keep only daily candles. The archiver saves 1-minute
candles (OHLC, volume, OI) of every listed option within +-25% of the stock price and of every
listed futures contract, into the same `data/market_nse.db`:

```bash
python -m app archive --budget 40000   # first run, e.g. overnight (a few hours at Kite's 3 requests a second)
python -m app archive --status         # what has been saved, and how much space it takes
```

Each contract is fetched twice: once when about 45 days are left (everything since it was listed),
and again in its series' last 3 trading days. Kite allows 60 days of 1-minute data per request, so
that is two requests per contract. If the laptop is off in expiry week, that series keeps everything
up to its first pass and the archiver says so; strikes that move into the band later are fetched with
their full history. The 16:10 job does this every evening, at most `archive.max_requests_per_run`
(12,000, about an hour) requests a night; most nights need far fewer. Stock bars (up to 2,000 days)
are kept up to date and each stock's continuous futures saved once. A stock's own intraday history
stays on Kite for years, so it is not archived yet.

Storage: only minutes with trades are kept, compressed per contract and day (7-15 bytes a candle
instead of ~100 as database rows). Expect roughly 100-450 MB a month (1-5 GB a year); the first
`--status` after a real run shows the actual figure. Daily candles are derived from the same data
for the research study. `archive.interval: day` switches back to daily candles only. Candles are
trade prices, not quotes; the recorder adds the real bid/ask.

### Intraday quote snapshots

While `python -m app run` is going, every 15 minutes from 09:32 to 15:17 (`snapshots:` in
config.yaml) the recorder saves bid/ask, top-of-book and 5-level sizes, volume and OI for the
strikes the strategy trades: options of each stock's two nearest monthly series within +-15% of the
futures price, plus those futures and the stock. That is about 45 quote requests per snapshot
(Kite: one a second, so under a minute), timed between the 5-minute monitor runs. Rows are compacted
into compressed blobs after the close: roughly 12 MB a day for ~200 stocks, about 3 GB a year
(`snapshots.every_min: 30` halves it, `0` turns it off). Together with the 1-minute candles this is what a future intraday replay
needs to test exits at the 5-minute marks they run on live. A failing snapshot never disturbs
trading: it is logged, and alerted at most once a day.

### First Indian study (stock prices only, ready for the first night)

```bash
python research/india_stock_study.py > research/results_india_stocks.md
```

Uses only the daily bars the archiver saves on its first run (about 5 years for every F&O stock and
the NIFTY 50). For every stock, every week, it asks what a 0.25-delta put and call would have faced
over the next 15 trading days: how often each strike was hit or touched by trend, momentum,
short-term reversal, recent shocks, market regime and beta; which volatility forecast is most
accurate; how put strikes cluster on bad market days; and whether our support/resistance levels
carry information. The hypotheses it tests, and what each result would change, are fixed in
`research/research_plan.md`.

### Research study on Indian data

`research/option_selling_study.py` measures where selling stock options pays (implied vs later
realised volatility, single options and vertical spreads held to expiry, the effect of each entry
filter, support/resistance). It runs on the US data (`--market us`, results in
`research/results_us.md`) and on the Indian store:

```bash
python research/option_selling_study.py --market nse > research/results_nse.md
```

On Indian data, each option-day is priced at its trade nearest 15:00 (within 30 minutes; `--at`,
`--window`) against the same expiry's futures at that minute, so a stale print cannot distort IV;
option-days that did not trade near 15:00 are left out. IV and delta come from those prices
(Black-76). Bid/ask comes from recorded quotes where they exist, otherwise from the spreads recorded
for that stock and delta (or `--assume-spread`, 4% of the price, until enough quotes are recorded).
An option enters the study once its expiry has passed. The output says how many completed expiries
it rests on: t-statistics need at least 8 monthly expiries, and anything under about 24 (two years)
is anecdotal.

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

## Strategies side by side (paper sleeves)

The main rules in `config.yaml` form the **main book**, the only one that can ever go live. Each entry
under `strategies:` is a **paper sleeve**: the main rules with some settings overridden, evaluated
on the same scan in its own paper book, with its own capital, risk limits, exits, equity curve and
drawdown pause. The manual kill switch stops every book. The shipped sleeve, `v2`, is the draft
from the research review:
- richness from IV percentile alone (no IV/HV test);
- the "shock has passed" timing rule;
- direction from the 50-day trend instead of support/resistance;
- 0.15-0.25 delta shorts with 3-4% wings, keeping 75% of the credit before taking profit;
- no thesis stop.

The scanner is built in layers so sleeves can differ in logic, not only in numbers:

| Layer | File | Per | What |
|---|---|---|---|
| Signals | `strategy/signals.py` | stock, shared | bars, ATM IV and percentile, levels, averages, recent moves, chains |
| Entry rules | `strategy/rules.py` | strategy | DTE, F2, F3 (0 = off), F4, F5 timing (`iv.timing`), direction (`direction.method`) |
| Structures | `strategy/structures.py` | strategy | strikes (F6, F7), wings (`spread.wing`: steps or % of spot), F8, F9, sizing, EV |
| Risk | `risk/` | book | limits, kill switches, equity and drawdown pause per book |

```bash
python -m app doctor                       # lists each sleeve and what it changes
python -m app status                       # every book's equity, entry status and open spreads
python -m app report --strategy all        # one weekly report per book, each with a comparison table
python -m app resume --drawdown --strategy v2
```

Alerts from a sleeve are prefixed with its name, e.g. `[v2] ENTRY ...`. Removing a sleeve from the
config stops its new entries; its open spreads are still managed, under the main rules.

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
  strategy/   scanner.py signals.py rules.py structures.py candidates.py spreads.py costs.py ev.py
  risk/       sizing.py limits.py equity.py
  execution/  paper.py entries.py router.py
  monitor/    marks.py exits.py replay.py
  reporting/  metrics.py charts.py weekly.py telegram_bot.py approvals.py notifier.py dashboard.py templates/
tests/
config.yaml  levels_manual.csv  events_override.csv  sectors_manual.csv  holidays_manual.csv  .env.example
```
