# Changelog

Threshold changes go here, in batches, only after every 30 closed trades (spec: tuning rule).
Each entry names the keys changed, old and new values, and the evidence.

## Unreleased

### Running unattended (20 Nov 2026)
- `python -m platform_core service install|status|logs|restart|stop|start|uninstall`: starts the platform with
  the laptop (Windows Task Scheduler at log-on, macOS launchd agent, Linux systemd user service) and restarts it
  if it stops. Only one platform process can run at a time.
- Once-a-day jobs now run through a daily runner (platform_core/daily.py): at their due time, or as soon as
  possible after a late start, a sleep or a late Kite login, before each job's deadline; missed jobs are
  reported, failed ones retried every 5 minutes, progress kept across restarts. The quote recorder is kept
  running in its window. Missed official re-checks and expiry settlements run the next morning (08:50).
- The laptop is kept awake on trading days from 08:35 to 18:55 (`service` in platform.yaml).

### Market-data repository (owner: "go with defaults", 20 Nov 2026)
- New `datalake/`: Parquet repository with a SQLite catalog under `lake.root` (`data/lake`). Nightly 1-minute
  candles with OI for every listed strike of the next 3 series and every stock future (passes at 45 and 20
  days and in the last 3 sessions), stock and index minutes with backfill to 2015, live minute quotes for
  ~9,000 instruments over Kite's WebSocket (3 connections, one process each), NSE's daily files (raw and
  parsed bhavcopies) with a 5-year bhavcopy backfill, an empirical check of NSE's OI unit, migration of the
  earlier archiver's data, `lake` commands and a Telegram summary at 18:40.
- New settings: `lake` in platform.yaml; `broker.margin_rps` was added earlier. In config.yaml,
  `archive.contracts: false` (new key): the earlier archiver keeps its daily bars and leaves option and futures
  candles to the repository, so nothing is fetched twice. The archiver's own tests and the study's pipeline
  test switch contracts on explicitly.
- The research study reads the repository (`--lake`, default `data/lake`) as well as the earlier store.
- New requirement: `duckdb`.

### Level-Based Credit Spreads platform (System Spec, 19 Nov 2026)
- New shared platform (`platform_core/`) and level-based strategy (`level_system/`), built in the spec's
  milestone order. Five paper accounts (`oi_tight`, `oi_loose`, `level_strict`, `level_research`,
  `discretionary`), ₹5,00,000 each from `go_live_date`, which is not set yet. One shared paper broker, one Kite
  session, a phone control centre, and a combined schedule. Paper trading only.
- `platform.yaml` holds the spec's template values unchanged. Added with the owner's approval (19 Nov 2026):
  `setups.s3_trend_pullback.min_pullback_sessions: 2`, `setups.s4_range_rejection.day0_within_sessions: 3`,
  `exits.event_exit: true` (level books), `settlement` (expiry settlement in every account, with
  `stt_exercise: 0.0015` and `physical_delivery_brokerage_pct: 0.0025`, plus the delivery-margin steps),
  `data.events_file_max_age_days: 1` (08:45 reminder) and `data.sectors_file_fresh_days: 90`. Operational
  additions: the schedule times, `control_centre.host` and `port`, `oi_system`, and `paths`.
- The earlier OI-wall system: its strategy rules are unchanged. Its fills and money records go through the
  shared broker (main -> `oi_tight`, v2 -> `oi_loose`), and its entries start on `go_live_date`.
  `costs.exchange_txn_pct` changes from 0.0003503 to 0.0003553 (0.03553% each side, owner-approved 19 Nov 2026,
  the same as the platform). New `broker.margin_rps: 5.0` rate-limits Kite basket-margin calls.
- To check on the laptop: Zerodha's physical-delivery margin steps (`settlement.delivery_margin_steps`,
  `delivery_margin_full_pct`) and delivery brokerage rate. support.zerodha.com was not reachable from the build
  machine.

### Strategies side by side (owner-approved restructure, 8 Nov 2026)
- The scanner is split into layers: shared signals per stock (`strategy/signals.py`), entry rules per
  strategy (`strategy/rules.py`), spread construction per strategy (`strategy/structures.py`), and the
  risk layer per book. The main rules behave exactly as before.
- Paper sleeves (`strategies:` in config.yaml): the main rules with overrides, each in its own paper
  book (own capital, limits, exits, equity, drawdown pause, config hash). Always paper. Candidates,
  trades and equity carry a `strategy` column (migration 0007; existing rows belong to `main`).
- New logic options: `iv.timing` (cause_passed | shock_passed | off), F3 off at `iv.min_iv_hv_ratio: 0`,
  `direction.method` (levels | trend, `trend_days`), `spread.wing` (steps | pct, `wing_pct`, `wing_tolerance`).
- Shipped sleeve `v2` (the research review's draft). Its wings are 3-4% of spot, not the 5-6% the US study
  favoured: with ~Rs 6-7 lakh per lot, one lot of a 5-6% spread risks more than the 5% per-trade gap cap on
  Rs 5 lakh. Its profit target keeps 75% of the credit (`exit.tp_frac: 0.25`; tp_frac is the cost to close
  as a share of the credit).
- Reports per book: `status`, scan output, heartbeat, weekly report (`report --strategy NAME|all`, with a
  side-by-side table), simulation and backtest summaries, dashboard (book selector);
  `resume --drawdown --strategy NAME`.

### Market-independent fixes from the research review (owner-approved, 8 Nov 2026)
These follow from option maths and trading costs, not from fitting US data; the spec's behaviour is one
setting away.
- F8 is now a friction test (`spread.f8_mode: friction`): net credit after all charges must be at least
  3x the cost of crossing both legs' bid-ask to close (`min_credit_friction`) and at least 5 ticks
  (`min_credit_ticks`). The spec's 25%-of-width rule (`f8_mode: width`) almost never passes together with
  F7's 0.20-0.30 delta band. Funnel stage renamed "F8 credit quality".
- Hard stop judged on the mid-price cost to close (`exit.stop_price: mid`); fills stay at natural prices.
  Prevents wide quotes alone from stopping trades out. The path-simulated EV uses the same rule.
- Optional cap on contracts per spread (`risk.max_lots_per_trade`, off by default).

### Indian data collection
- Kite history archiver (`python -m app archive`, nightly at 16:10 within `archive.max_requests_per_run`):
  saves candles of listed stock options within +-25% of the price and every listed futures contract
  before Kite drops them, plus continuous futures and up to 2,000 days of stock bars. Resumable;
  respects Kite's rate limit.
- Owner decision, 8 Nov 2026: archive at 1-minute resolution (`archive.interval: minute`), because
  expired contracts cannot be fetched again and 1-minute data can always be turned into coarser data.
  Each contract is fetched twice (first pass at 45 days left, `archive.first_pass_dte`; final pass in
  its last 3 trading days), which fits Kite's 60-day limit per 1-minute request; a missed final pass is
  reported. Only traded minutes are stored, compressed per contract-day (`app/data/barcodec.py`);
  daily candles are derived from them. The request budget counts real Kite requests after splitting
  at Kite's span limits; `max_requests_per_run` raised from 5,000 to 12,000.
- Intraday quote snapshots (`snapshots:`): every 15 minutes in the session, bid/ask, sizes, volume
  and OI for options within +-15% of the futures price in each stock's two nearest series, plus
  futures and stocks; compacted after the close. Failures never disturb trading.
- The study prices each option at its trade nearest 15:00 against the futures at the same minute,
  and drops option-days with no trade near 15:00.
- Quote snapshots also save all pending buy and sell quantity and the last trade's size (order-book
  imbalance, for the retail-demand hypotheses). The archiver keeps NIFTY 50 daily bars
  (`archive.index`) for beta.
- First Indian study (`research/india_stock_study.py`), stock prices only: strike hit rates by trend,
  momentum, reversal, shocks, market regime and beta; volatility-forecast accuracy (HV20, HV60, EWMA,
  Yang-Zhang, HAR); put-strike clustering on bad market days; the level test. Hypotheses and decision
  rules fixed beforehand in `research/research_plan.md`.
- The research study runs on Indian data (`research/option_selling_study.py --market nse`): IV and delta
  from daily option closes against the same expiry's futures (vectorised Black-76), bid/ask from recorded
  quotes or from spreads learnt from them, results dates as first seen, and windows across bonus, split,
  rights or demerger dates left out.
- Study fix (both markets): holding windows whose expiry lies after the last stored close are left out;
  they had been settled at the last close. US figures in `research/results_us.md` and
  `research/strategy_review.md` refreshed (about 2% of rows; no conclusion changed).

### Real-data backtest and architecture (8 Nov 2026)
- `python -m app backtest load|run`: DoltHub US end-of-day option quotes, earnings and splits replayed
  through the live code path (HistoricalBroker, HistoricalExchange, runner); `--set` overrides for
  variants. Market profile (`market.name`, `market.currency`) and a per-contract cost model.
- Scan quotes are prefetched in batched broker calls (at most ~4 calls per scan instead of 2-4 per
  stock): about a minute for ~220 stocks at Kite's one request a second, instead of 7-15 minutes.
- NSE client no longer impersonates a browser or works around NSE's bot check; when NSE refuses,
  F2 fails closed and event CSV exports dropped into `imports/events/` keep F2 working.
- Every candidate and trade records `config_hash`; `config_versions` stores the full thresholds.
- End-of-day: daily SQLite backup (`ops.backup_keep`), heartbeat alert (`ops.heartbeat`).
- Live market recorder (`ops.record_market`): each scan's Kite quotes, sizes, OI, volume, lot sizes and
  futures prices, plus bars, sectors and first-seen event dates, saved to `paths.market_store`;
  `backtest run --market nse` replays it.
- Faster end-of-day indicators (ATR in numpy, HV percentile only for new rows).

### Owner decisions, 6 Nov 2026 (before any paper trades)
- Risk is sized by the loss at the hard stop, not by maximum loss: `risk.sizing_basis: stop`.
  Risk per share = (stop_mult - 1) x credit x `risk.stop_overshoot` (1.25, because stops fill past
  the trigger), capped at max loss. `risk.per_trade` (2%) and `risk.max_total` (8%) now apply to it.
- Gap guard kept on full max loss, since a gap can skip the stop: `risk.gap_cap_per_trade: 0.05`,
  `risk.gap_cap_total: 0.20` (null disables).
- Universe: every F&O stock (`universe.top_n: null`, was 50); no turnover ranking.
- Liquidity judged in absolute terms per option leg: two-sided quote, bid-ask <= 5% of mid, and the
  5-level book on bid and ask of both legs must cover the order size (`liquidity.min_depth_cover: 1.0`).
  OI and volume minimums off (`min_oi_lots: 0`, `min_vol_lots: 0`, were 500 / 50).
- EV now comes from a path simulation of the exit rules (`ev.method: paths`): 2,000 price paths at
  the forecast vol, checked 6 times a day for the hard stop, the thesis stop (daily) and the profit
  target, closed at the time stop otherwise; legs repriced with Black-76 at entry IV, natural prices
  via the entry half-spreads. p_model is the share of paths that make money after costs. The spec's
  expiry formula stays available as `ev.method: expiry`. Example: 470/460 put on a 500 stock, credit
  2.60, IV 40%, HV 30%: expiry formula EV 18.3% of max loss; with the exit rules about 1.5%.

### Milestones 2-7
- Broker and data layer, analytics, levels and events, scanner, paper engine with monitor and
  scheduler, weekly report, Telegram alerts and commands, optional dashboard.
- Defaults set: `costs.exchange_txn_pct: 0.0003503` (NSE 0.03503% of premium, zerodha.com/charges,
  Nov 2026).
- Additions beyond the spec, all configurable: unannounced results estimated from the last results
  date fail F2 (`events.estimate_results`); missed 5-minute marks are replayed from candles
  (`monitor.replay_missed`); proposals for live approval expire (`live.approval_timeout_min`).

### Milestone 1: skeleton
- Repository layout, validated `config.yaml`, SQLite schema (migration 0001), logging, `.env` loading,
  live-trading guard, `python -m app doctor` and `python -m app migrate`.
- Config keys added beyond the spec template, for numbers the spec states in prose:
  `universe.turnover_window`, `iv.spike_move_atr`, `iv.atm_min_dte`, `direction.max_dist_atr`,
  `analytics.*`, `paper.margin_fallback_mult`, `schedule.*`, `killswitch.*`, `paths.*`.
