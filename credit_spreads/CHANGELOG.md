# Changelog

Threshold changes go here, in batches, only after every 30 closed trades (spec: tuning rule).
Each entry names the keys changed, old and new values, and the evidence.

## Unreleased

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
