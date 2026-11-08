# Changelog

Threshold changes go here, in batches, only after every 30 closed trades (spec: tuning rule).
Each entry names the keys changed, old and new values, and the evidence.

## Unreleased

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
