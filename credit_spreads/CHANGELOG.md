# Changelog

Threshold changes go here, in batches, only after every 30 closed trades (spec: tuning rule).
Each entry names the keys changed, old and new values, and the evidence.

## Unreleased

### Milestone 1: skeleton
- Repository layout, validated `config.yaml`, SQLite schema (migration 0001), logging, `.env` loading,
  live-trading guard, `python -m app doctor` and `python -m app migrate`.
- Config keys added beyond the spec template, for numbers the spec states in prose:
  `universe.turnover_window`, `iv.spike_move_atr`, `iv.atm_min_dte`, `direction.max_dist_atr`,
  `analytics.*`, `paper.margin_fallback_mult`, `schedule.*`, `killswitch.*`, `paths.*`.
