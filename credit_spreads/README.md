# Stock Credit Spread System

Paper trading only: no code here places a real order.

This folder holds two strategies that run side by side on one shared platform:

- **The level-based credit spreads system** (spec: *Level-Based Credit Spreads — System Spec*, 19 Nov 2026). It
  trades each spread beyond a price level that four rule-based setups say should hold. It has a strict book and
  a research book.
- **The earlier OI-wall system** (the rest of this README, from "Setup" down). It runs unchanged, with its two
  books traded as accounts `oi_tight` (main rules) and `oi_loose` (v2 rules).

All five paper accounts start with ₹5,00,000 on `go_live_date` and trade through one shared paper broker. The
fifth account, `discretionary`, holds the owner's own trades. A phone control centre shows every account and
places discretionary trades.

## The platform

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Skeleton and shared core: `platform.yaml` (pydantic), SQLite + Parquet, `.env` | done |
| 2 | Shared data service: one Kite session, NSE bhavcopy, events, sectors, mock broker and fixtures | done |
| 3 | Paper broker: fills, Zerodha costs, margin with the delivery step-up, rejections, per-account ledger | done |
| 4 | The OI-wall system as `oi_tight` and `oi_loose` (its own tests still pass) | done |
| 5 | Analytics: ATR, swings, VWAP, HVN, Nifty trend, zone OI share, local PCR, Black-76 repricing | done |
| 6 | Setup detectors (four setups and mirrors) and the setup state machine | done (real-day tests wait for fixtures) |
| 7 | Trading engine: screener, ROI ranking, strict and research books, strikes, sizing, entries, exits, R | done |
| 8 | Control centre (phone): overview, positions and funds, order ticket, ROI scanner, journal and reports | done |
| 9 | Scheduler, holidays, Telegram alerts, daily screener report, monthly comparison | done |

### What the owner provides (never in chat: only in `.env` on the laptop)

1. **Kite Connect** (paid plan): `KITE_API_KEY` and `KITE_API_SECRET` from developers.kite.trade. Set the app's
   redirect URL to `http://127.0.0.1:5000/kite/callback`.
2. **Telegram**: `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID` (your chat with the bot).
3. **Control-centre PIN**: `python -m platform_core set-pin`. It stores only a salted hash and a cookie secret.
4. **Tailscale** on the laptop and the phone (below).

### Setup on the laptop

Use Python 3.11 or newer (Windows, macOS or Linux).

```bash
git clone -b claude/document-review-setup-iy6jnq https://github.com/divu1702/Systematic-Portfolio-Attribution-Risk-Engine
cd Systematic-Portfolio-Attribution-Risk-Engine/credit_spreads
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # Windows: copy .env.example .env  (then fill it in)
python -m platform_core set-pin
python -m platform_core doctor                           # what is missing; never prints a secret
python -m pytest                                         # the whole suite, offline
python -m platform_core login                            # first Kite login
python -m platform_core record-fixtures                  # saves ANGELONE 17 Nov and Kalyan 11 Nov candles
python -m pytest tests/test_level_realdays.py            # the spec's two real-day detector tests
git add tests/fixtures/realdays && git commit -m "Real-day fixtures"   # keep them with the code
```

Then leave `python -m platform_core run` running from 08:40 to 18:35 on trading days, with the laptop plugged
in, sleep off and on wifi. That one process runs the scheduler for both systems, the control centre on
`127.0.0.1:8765`, and the Telegram bot (`/token`, `/status`, `/kill` …).

### Tailscale: the phone reaches the laptop privately

The control centre listens only on `127.0.0.1`. Tailscale's `serve` gives it a private HTTPS address on your
tailnet: only your own devices can reach it, and it is never on the public internet. Do not use
`tailscale funnel`, which would publish it to the internet.

1. Install Tailscale on the laptop and sign in.
   - **Windows:** the installer from tailscale.com/download, then sign in from the tray icon. Run the commands
     below in PowerShell (`tailscale` is on the PATH after installing).
   - **macOS:** the Tailscale app (Mac App Store or the standalone package), then sign in. The CLI is
     `/Applications/Tailscale.app/Contents/MacOS/Tailscale` unless you install it from the app's settings.
   - **Linux:** `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`.
2. Install the Tailscale app on the phone (Play Store or App Store) and sign in with the same account.
3. In the Tailscale admin console (login.tailscale.com, DNS page), enable MagicDNS and HTTPS certificates.
4. On the laptop, once: `tailscale serve --bg --https=443 http://127.0.0.1:8765` (with `sudo` on Linux).
   `tailscale serve status` shows the address, `https://<laptop-name>.<tailnet>.ts.net`. Open it on the
   phone and add it to the home screen. The setting survives restarts; `tailscale serve reset` removes it.
5. Enter the PIN. Five wrong PINs lock logins for 15 minutes. The session lasts 12 hours.

### Daily schedule (IST, NSE trading days)

| When | What |
|------|------|
| 08:30 | Instrument master, holidays, sectors (both systems read them) |
| 08:45 | Telegram: the Kite login link (one login for both systems), and a reminder if the newest events file is more than 1 day old |
| 09:20 | OI-wall gap check (its own rules) |
| 09:25 | Margin recompute for every open position: basket margin, plus the delivery step-up from the Wednesday before expiry |
| 09:30 | Level system gap check: an overnight gap beyond a level exits at once |
| every 5 min | OI-wall monitor (its own exits) |
| every 30 min, 10:00-15:00 | Level system exit checks (profit target; overdue time or event exits) |
| 15:00 | OI-wall scan; its entries start on `go_live_date` |
| 15:20 | Level system: exits on the snapshot, screener, confirmations, entries, ROI ranking, daily report and Telegram summary |
| 15:35, 16:10 | OI-wall end of day; history archiver |
| 18:30 | Bhavcopy download and official re-check (`close_mismatch`), expiry settlement for every account, monthly comparison after the month's last session |

The control centre's pages refresh every 30 seconds. Telegram carries alerts only: entries, exits, rejections
and data failures.

### Going live

The system runs fully before `go_live_date`: setups arm and confirm, the screener ranks stocks, and the daily
report shows what each book would have entered (marked "dry run"). No account trades yet. To start all five
accounts together, set `go_live_date: YYYY-MM-DD` in `platform.yaml` and restart. After that, the strategy rules
stay fixed until 30 strict-book trades have closed, apart from critical fixes. Record every change in
`CHANGELOG.md`.

### Data the platform keeps

| Where | What |
|-------|------|
| `data/platform.db` | accounts, orders, ledger, trades (every account), marks, levels, setups, screener_daily, close_checks, account_days, uploads |
| `data/chain_snap/date=YYYY-MM-DD/*.parquet` | every option quote the 15:20 run looked at, with IV |
| `data/trading.db` | the OI-wall system's own records (its trades mirror the broker's, same trade ids) |
| `reports/platform/daily/`, `monthly/`, `runs/` | screener reports, monthly comparisons, each run's JSON record |
| `imports/events/`, `imports/bhavcopy/`, `sectors.csv` | files uploaded from the phone (or saved by hand) |

`--mock` runs any command on the synthetic market with separate databases (`*_mock.db`).
`python -m platform_core job close|gap|exits|margins|official|monthly` runs one job now.

### Readings of the spec built in

These are the owner's answers of 19 Nov 2026, plus a few readings of my own where the spec is silent.
Every one can be changed.

- Each book enters at the first session that qualifies for it, in separate accounts. Only levels that no close
  has broken count. That applies to major swings, and here also to each Setup 4 range edge.
- Setup 2, Route B: its three closes are the three sessions after Day 0, and the zone OI test must pass on
  each of them. A close back below the level before any retest cancels the setup. If Route B's OI test fails,
  the research book enters at the third close and Route A stays open for the strict book.
- Setup 3: the pullback runs at least 2 sessions after the highest high since the last minor swing low. Its
  level is the lowest low from the pullback through Day 0.
- Setup 4: Day 0 falls on the touch session or within the 3 sessions after it. A move that also qualifies as
  Setup 1 is treated as Setup 1.
- Setup 1 zone: from the strike at or just below the level up to the strike at or just above Day 0's high. The
  other side is the 3 strikes below that. For Setups 2-4, the 3 strikes strictly beyond the level on the
  protected side, and the 3 at or beyond it on the other side.
- The zone OI test stays on Day 0's expiry throughout confirmation. Snapshots are compared with the Day 0
  snapshot. The 18:30 official values only log `close_mismatch`; they never undo a trade.
- ATR is the value known at the session's open: Wilder's ATR on completed sessions.
- A session the laptop missed is judged from the daily candles, without OI. It never produces a late entry.
- Screener liquidity: a stock is removed if either side's strike nearest 1 x ATR fails.
- The strict book's one-per-sector rule treats an unknown sector as one sector.
- Positions still open at expiry settle at intrinsic value in every account. The settlement charges STT on
  exercise (0.15% of the bought leg's intrinsic value) and physical-delivery brokerage (0.25% of strike x
  quantity per in-the-money leg, plus GST). Check the delivery-margin steps (10/25/45/70% of 20% from the
  Wednesday before expiry) and the brokerage rate against Zerodha's policy page: it was not reachable from the
  build machine.
- Event exit (level books): when a results date or board meeting falls on or before the trade's time-stop
  date, the trade exits at the 15:20 snapshot of the last session before the event, or at once if that
  session has passed.
- 1R: for the OI-wall accounts it is the planned loss at their own stop. For discretionary trades it is the
  loss at the owner's stop level, priced like the kill level, and the spread's maximum loss when no stop is
  given.

## The earlier OI-wall system

On the platform, `python -m platform_core run` replaces `python -m app run`: it runs this system's jobs unchanged
(its scan enters trades only from `go_live_date`) and sends one 08:45 login link for both systems.

It scans liquid NSE stock options daily for defined-risk credit spreads (bull put, bear call) when implied
volatility is unusually rich. On the platform its orders fill through the shared paper broker.

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Skeleton: config, SQLite schema, logging, `doctor` | done |
| 2 | Broker and data layer (Kite Connect, MockBroker) | done |
| 3 | Analytics (Black-76, IV solver, HV, ATR) | done |
| 4 | Levels and events | done |
| 5 | Scanner (F1–F9, EV ranking) | done |
| 6 | Paper engine, monitor, scheduler | done |
| 7 | Reporting and Telegram alerts | done |
| 8 | Live adapter | not built: the platform is paper trading only |

### Setup (on the laptop that will run it)

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
   Actions CSVs from nseindia.com in your browser and drop them into `imports/events/` every day or
   two: each file counts as fresh for `events.max_staleness_days` (2) days after you save it, and after
   that F2 blocks every entry. Or use a licensed data feed.
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
platform_core/   the shared platform
  config.py (platform.yaml)  db.py models.py migrations/  parquet_store.py  data_service.py  nse_files.py
  fills.py costs.py broker.py accounts.py   oi_bridge.py (the OI-wall system as oi_tight / oi_loose)
  discretionary.py  runtime.py  scheduler.py  cli.py doctor.py  control/ (FastAPI app, auth, templates)
level_system/    the level-based strategy
  analytics.py setups.py state.py store.py strikes.py sizing.py screener.py engine.py report.py comparison.py fixtures.py
app/             the earlier OI-wall system
  config.py env.py safety.py clock.py context.py cli.py doctor.py jobs.py scheduler.py sim.py market.py views.py
  broker/     base.py kite.py login.py mock.py synthetic.py
  data/       db.py models.py migrations/ instruments.py nse.py events.py daily.py levels_store.py refdata.py ...
  analytics/  black76.py vol.py levels.py
  strategy/   scanner.py signals.py rules.py structures.py candidates.py spreads.py costs.py ev.py
  risk/       sizing.py limits.py equity.py
  execution/  paper.py entries.py router.py
  monitor/    marks.py exits.py replay.py
  reporting/  metrics.py charts.py weekly.py telegram_bot.py approvals.py notifier.py dashboard.py templates/
tests/          (tests/fixtures/realdays/ holds the recorded real days)
platform.yaml  config.yaml  levels_manual.csv  events_override.csv  sectors_manual.csv  holidays_manual.csv  .env.example
```
