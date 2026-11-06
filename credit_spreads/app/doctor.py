"""`python -m app doctor`: print config and database status, and flag anything not ready."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, TextIO

from . import __version__
from .config import Config, ConfigError, load_config
from .data.db import db_status
from .env import DEFAULT_ENV_PATH, KNOWN_SECRETS, load_env
from .fmt import inr, pct
from .safety import LIVE_CONFIRM_ENV, live_trading_allowed

OK, WARN, FAIL = "ok", "WARN", "FAIL"


@dataclass
class Report:
    out: TextIO
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def heading(self, title: str) -> None:
        self.out.write(f"\n{title}\n{'-' * len(title)}\n")

    def row(self, label: str, value: object, status: str = "") -> None:
        tag = f"  [{status}]" if status else ""
        self.out.write(f"  {label:<28} {value}{tag}\n")

    def warn(self, label: str, value: object, why: str) -> None:
        self.row(label, value, WARN)
        self.warnings.append(f"{label}: {why}")

    def fail(self, label: str, value: object, why: str) -> None:
        self.row(label, value, FAIL)
        self.errors.append(f"{label}: {why}")


def _config_section(r: Report, cfg: Config, env: Mapping[str, str]) -> None:
    r.heading("Mode")
    if cfg.mode == "paper":
        r.row("mode", "PAPER (live order paths unreachable)", OK)
    elif live_trading_allowed(cfg, env):
        r.warn("mode", "LIVE, confirmation flag set: REAL ORDERS ENABLED", "live trading is enabled")
    else:
        r.warn("mode", f"live, but {LIVE_CONFIRM_ENV} not set: real orders blocked",
               "mode is live without the confirmation flag")
    r.row("capital", inr(cfg.capital))

    r.heading("Entry filters")
    u = cfg.universe
    r.row("F1 universe", (f"top {u.top_n} by {u.turnover_window}-day option turnover" if u.top_n else
                          "all F&O stocks") + (f", turnover >= {inr(u.min_avg_turnover)}/day" if u.min_avg_turnover else ""))
    r.row("F2 event buffer", f"expiry + {cfg.events.buffer_days}d, stale after {cfg.events.max_staleness_days}d")
    r.row("F3 IV / HV20", f">= {cfg.iv.min_iv_hv_ratio}")
    r.row("F4 IV percentile", f">= {cfg.iv.min_ivp:g} over {cfg.iv.ivp_history_days}d "
          f"(proxy until {cfg.iv.ivp_min_history}d: IV > HV p{cfg.iv.proxy_hv_pctile:g})")
    r.row("F5 spike lookback", f"{cfg.iv.spike_lookback}d, move >= {cfg.iv.spike_move_atr} x ATR")
    r.row("F6 level buffer", f">= {cfg.levels.min_buffer_atr} x ATR")
    r.row("F7 short strike", f"delta {cfg.strikes.delta_min:.2f}-{cfg.strikes.delta_max:.2f}, "
          f">= {cfg.strikes.min_dist_atr} x ATR from spot")
    r.row("F8 credit / width", f">= {pct(cfg.spread.min_credit_width, 0)}")
    lc = cfg.liquidity
    r.row("F9 liquidity", f"bid-ask <= {pct(lc.max_spread_pct, 0)} of mid, 5-level depth >= {lc.min_depth_cover:g}x order "
          f"on both sides" + (f", OI >= {lc.min_oi_lots} lots" if lc.min_oi_lots else "")
          + (f", vol >= {lc.min_vol_lots} lots" if lc.min_vol_lots else ""))
    r.row("DTE window", f"{cfg.dte_min}-{cfg.dte_max} calendar days")
    r.row("EV", f"hv_weight {cfg.ev.hv_weight}, EV/max loss >= {pct(cfg.ev.min_ev_ratio)}")

    r.heading("Exits and risk")
    r.row("exits", f"stop {cfg.exit.stop_mult}x credit, target {pct(cfg.exit.tp_frac, 0)}, "
          f"time stop T-{cfg.exit.days_before_expiry} trading days")
    rk = cfg.risk
    basis = (f"loss at stop ({cfg.exit.stop_mult - 1:g}x credit x {rk.stop_overshoot:g})" if rk.sizing_basis == "stop"
             else "max loss")
    r.row("sizing basis", basis)
    r.row("per trade / total", f"{pct(rk.per_trade)} / {pct(rk.max_total)} of equity")
    r.row("gap caps (max loss)", f"{pct(rk.gap_cap_per_trade) if rk.gap_cap_per_trade else 'off'} per trade / "
          f"{pct(rk.gap_cap_total) if rk.gap_cap_total else 'off'} total")
    r.row("max open/same side/sector",
          f"{cfg.risk.max_open} / {cfg.risk.max_same_direction} / {cfg.risk.max_per_sector}")
    r.row("daily stop / drawdown pause", f"{pct(cfg.risk.daily_stop)} / {pct(cfg.risk.dd_pause)}")
    r.row("live order rate cap", f"{cfg.live.max_ops} orders/sec")

    r.heading("Costs")
    c = cfg.costs
    r.row("brokerage per order", inr(c.brokerage_per_order))
    r.row("STT on sold premium", pct(c.stt_sell_premium, 3))
    if c.exchange_txn_pct is None:
        r.warn("exchange txn charge", "NOT SET",
               "fill costs.exchange_txn_pct from the current NSE circular; "
               "the cost model will refuse to price trades until then")
    else:
        r.row("exchange txn charge", pct(c.exchange_txn_pct, 4))
    r.row("SEBI fee", f"{inr(c.sebi_fee_per_crore)} per crore")
    r.row("stamp duty (buy)", pct(c.stamp_buy_pct, 4))
    r.row("GST", pct(c.gst, 0))


def _secrets_section(r: Report, env: Mapping[str, str], env_path: Path) -> None:
    r.heading("Secrets")
    r.row(".env file", env_path if env_path.exists() else f"{env_path} (not found)",
          OK if env_path.exists() else "")
    for key, needed_for in KNOWN_SECRETS.items():
        # Never print the value, only whether it is set.
        r.row(key, "set" if env.get(key) else f"missing, needed from {needed_for}")


def _db_section(r: Report, cfg: Config) -> None:
    r.heading("Database")
    r.row("path", cfg.db_path)
    st = db_status(cfg.db_path)
    if not st["exists"]:
        r.warn("database", "not created", "run `python -m app migrate` to create the database")
        return
    v, latest = st["version"], st["latest"]
    if v < latest:
        r.warn("schema version", f"{v} of {latest}", "run `python -m app migrate`")
    elif v > latest:
        r.fail("schema version", f"{v} of {latest}", "database is newer than this code")
    else:
        r.row("schema version", f"{v} (latest)", OK)
    r.row("size", f"{st['size_kb']:.0f} KB")
    for name, n in st["tables"].items():
        if n < 0:
            r.fail(f"table {name}", "missing", "table missing from database")
        else:
            r.row(f"table {name}", f"{n} rows")


def run_doctor(config_path: Optional[Path], out: TextIO, env_path: Optional[Path] = None) -> int:
    r = Report(out)
    out.write(f"Credit Spread System v{__version__} — doctor\n")
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        r.heading("Config")
        r.fail("config", "INVALID", str(exc))
        out.write(f"\n{exc}\n")
        out.write("\nResult: FAIL (fix config.yaml first)\n")
        return 2

    env_path = env_path or DEFAULT_ENV_PATH
    try:
        env = load_env(env_path)
    except ValueError as exc:
        env = {}
        r.errors.append(str(exc))

    r.heading("Config")
    r.row("file", cfg.base_dir / "config.yaml" if config_path is None else Path(config_path).resolve(), OK)
    _config_section(r, cfg, env)
    _secrets_section(r, env, env_path)
    _db_section(r, cfg)

    out.write("\n")
    for w in r.warnings:
        out.write(f"warning: {w}\n")
    for e in r.errors:
        out.write(f"error:   {e}\n")
    status = "FAIL" if r.errors else "OK"
    out.write(f"\nResult: {status} ({len(r.errors)} errors, {len(r.warnings)} warnings)\n")
    return 1 if r.errors else 0
