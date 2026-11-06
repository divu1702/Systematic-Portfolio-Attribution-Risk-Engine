# Changelog

Threshold changes go here, in batches, only after every 30 closed trades (spec: tuning rule).
Each entry names the keys changed, old and new values, and the evidence.

## Unreleased

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
