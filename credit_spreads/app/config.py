"""Pydantic-validated loader for config.yaml.

Every threshold the system uses comes from here. Unknown keys are rejected so that a
typo in config.yaml fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Reusable constrained types.
Fraction = Annotated[float, Field(gt=0, lt=1)]          # strictly between 0 and 1
Percentile = Annotated[float, Field(ge=0, le=100)]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(gt=0)]
NonNegFloat = Annotated[float, Field(ge=0)]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """Raised when config.yaml is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseCfg(_Section):
    # Owner's choice (Nov 2026): scan every F&O stock and judge liquidity in absolute terms at the
    # option level (F9), not by relative ranking. Set top_n to rank by option turnover instead.
    top_n: Optional[PosInt] = None
    turnover_window: PosInt = 20
    min_avg_turnover: NonNegFloat = 0.0       # rupees/day of stock-option premium turnover; 0 = off


class EventsCfg(_Section):
    buffer_days: NonNegInt = 1
    max_staleness_days: NonNegInt = 2
    fetch_ahead_days: PosInt = 60
    fetch_behind_days: PosInt = 120
    # Fail-closed extra: if no results date is announced yet, estimate the next one from the
    # last known results date + cycle, and treat it as an event if it lands near the window.
    estimate_results: bool = True
    results_cycle_days: PosInt = 91
    results_tolerance_days: NonNegInt = 10


class IVCfg(_Section):
    min_iv_hv_ratio: Annotated[float, Field(gt=0)] = 1.25
    min_ivp: Percentile = 70
    ivp_history_days: PosInt = 252
    ivp_min_history: PosInt = 120
    proxy_hv_pctile: Percentile = 80
    spike_lookback: PosInt = 10
    spike_move_atr: Annotated[float, Field(gt=0)] = 1.5
    atm_min_dte: NonNegInt = 10

    @model_validator(mode="after")
    def _history(self) -> "IVCfg":
        if self.ivp_min_history > self.ivp_history_days:
            raise ValueError("iv.ivp_min_history cannot exceed iv.ivp_history_days")
        return self


class LevelsCfg(_Section):
    fractal_n: PosInt = 5
    lookback: PosInt = 250
    cluster_atr: Annotated[float, Field(gt=0)] = 0.5
    min_buffer_atr: NonNegFloat = 0.25
    recency_halflife: PosInt = 60          # bars; a touch this old counts half
    weekly_weight: Annotated[float, Field(gt=0)] = 2.0
    manual_score: Annotated[float, Field(gt=0)] = 10.0


class DirectionCfg(_Section):
    max_dist_atr: Annotated[float, Field(gt=0)] = 1.5


class StrikesCfg(_Section):
    delta_min: Fraction = 0.20
    delta_max: Fraction = 0.30
    min_dist_atr: NonNegFloat = 1.0

    @model_validator(mode="after")
    def _delta_band(self) -> "StrikesCfg":
        if self.delta_min >= self.delta_max:
            raise ValueError("strikes.delta_min must be below strikes.delta_max")
        return self


class SpreadCfg(_Section):
    widths_steps: List[PosInt] = Field(default_factory=lambda: [1, 2, 3, 4], min_length=1)
    # F8. "friction" (owner-approved fix, Nov 2026): the net credit after all charges must be at least
    # `min_credit_friction` x the cost of crossing both legs' bid-ask to close, and at least
    # `min_credit_ticks` ticks. "width" is the spec's rule: credit >= min_credit_width x width, which
    # almost never passes together with F7's 0.20-0.30 delta band.
    f8_mode: Literal["friction", "width"] = "friction"
    min_credit_friction: Annotated[float, Field(gt=0)] = 3.0
    min_credit_ticks: NonNegInt = 5
    min_credit_width: Fraction = 0.25

    @field_validator("widths_steps")
    @classmethod
    def _unique_sorted(cls, v: List[int]) -> List[int]:
        if len(set(v)) != len(v):
            raise ValueError("spread.widths_steps must not contain duplicates")
        return sorted(v)


class EVCfg(_Section):
    hv_weight: Annotated[float, Field(ge=0, le=1)] = 0.5
    min_ev_ratio: NonNegFloat = 0.03
    # Owner: "paths" simulates the exit rules along price paths; "expiry" is the spec's formula.
    method: Literal["paths", "expiry"] = "paths"
    n_paths: Annotated[int, Field(ge=100, le=100_000)] = 2000
    steps_per_day: Annotated[int, Field(ge=1, le=75)] = 6    # rule checks per simulated day
    seed: NonNegInt = 7


class LiquidityCfg(_Section):
    max_spread_pct: Fraction = 0.05
    min_oi_lots: NonNegInt = 0                # 0 = off (spec default 500); depth check below instead
    min_vol_lots: NonNegInt = 0               # 0 = off (spec default 50)
    # 5-level book on bid and ask of both legs must hold this multiple of the order size.
    min_depth_cover: Annotated[float, Field(gt=0)] = 1.0


class AnalyticsCfg(_Section):
    hv_window: PosInt = 20
    atr_window: PosInt = 14
    trading_days_per_year: PosInt = 252
    risk_free: Annotated[float, Field(ge=0, lt=1)] = 0.065
    iv_min: Annotated[float, Field(gt=0)] = 0.01
    iv_max: Annotated[float, Field(gt=0)] = 5.0


class BrokerCfg(_Section):
    provider: Literal["kite", "mock"] = "kite"
    redirect_url: str = "http://127.0.0.1:5000/kite/callback"
    quote_batch: Annotated[int, Field(ge=1, le=500)] = 500
    quote_rps: Annotated[float, Field(gt=0, le=1)] = 1.0     # Kite quote API limit: 1 req/s
    hist_rps: Annotated[float, Field(gt=0, le=3)] = 3.0      # Kite historical API limit: 3 req/s
    mock_seed: NonNegInt = 7
    mock_symbols: Annotated[int, Field(ge=1, le=60)] = 20


class ScanCfg(_Section):
    top_n: PosInt = 5


class MonitorCfg(_Section):
    replay_missed: bool = True


class ReportingCfg(_Section):
    weekly_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "fri"
    weekly_time: str = "16:00"
    out_dir: str = "reports"

    @field_validator("weekly_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"expected HH:MM (24h IST), got {v!r}")
        return v


class ExitCfg(_Section):
    stop_mult: Annotated[float, Field(gt=1)] = 2.0
    tp_frac: Fraction = 0.50
    days_before_expiry: NonNegInt = 5
    thesis_stop: bool = True
    on_new_event: bool = True
    # Hard-stop trigger price. "mid" (owner-approved fix, Nov 2026): the mid-price cost to close, so a
    # wide quote alone cannot stop a trade out; fills are still at natural prices. "natural": the spec.
    stop_price: Literal["mid", "natural"] = "mid"


class RiskCfg(_Section):
    # "stop": risk = loss if the hard stop fires (stop_mult - 1) x credit x stop_overshoot, capped at
    # max loss. "max_loss": risk = width - credit (the spec's original rule). per_trade and
    # max_total apply to this basis.
    sizing_basis: Literal["stop", "max_loss"] = "stop"
    stop_overshoot: Annotated[float, Field(ge=1)] = 1.25   # stops fill past the trigger
    per_trade: Fraction = 0.02
    max_open: PosInt = 5
    max_same_direction: PosInt = 3
    max_total: Fraction = 0.08
    # Gap guard: a gap can jump straight past the stop to full max loss. Null disables.
    gap_cap_per_trade: Optional[Fraction] = 0.05
    gap_cap_total: Optional[Fraction] = 0.20
    max_per_sector: PosInt = 1
    max_lots_per_trade: Optional[PosInt] = None      # optional hard cap on contracts per spread
    max_margin_util: Annotated[float, Field(gt=0, le=1)] = 0.50
    daily_stop: Fraction = 0.03
    dd_pause: Fraction = 0.10

    @model_validator(mode="after")
    def _consistent(self) -> "RiskCfg":
        if self.max_same_direction > self.max_open:
            raise ValueError("risk.max_same_direction cannot exceed risk.max_open")
        if self.per_trade > self.max_total:
            raise ValueError("risk.per_trade cannot exceed risk.max_total")
        return self


class PaperCfg(_Section):
    extra_slip_ticks: NonNegInt = 1
    margin_fallback_mult: Annotated[float, Field(ge=1)] = 1.5


class LiveCfg(_Section):
    require_approval: bool = True
    # Spec: hard-cap at 2 orders/second, well under SEBI's 10 OPS registration threshold.
    max_ops: Annotated[int, Field(ge=1, le=2)] = 2
    step_secs: PosInt = 20
    entry_steps: PosInt = 3
    stop_steps: PosInt = 6
    approval_timeout_min: PosInt = 20     # an unanswered proposal expires after this

    @field_validator("require_approval")
    @classmethod
    def _v1_semi_auto(cls, v: bool) -> bool:
        if not v:
            raise ValueError("live.require_approval must be true: live entries are semi-automatic in v1")
        return v


class ScheduleCfg(_Section):
    instruments_refresh: str = "08:30"
    events_fetch: str = "08:45"
    login_reminder: str = "09:00"
    gap_check: str = "09:20"
    scan: str = "15:00"
    thesis_exit: str = "15:15"
    eod: str = "15:35"
    archive: str = "16:10"
    monitor_every_min: PosInt = 5
    no_entry_start: str = "09:15"
    no_entry_end: str = "09:20"

    @field_validator("instruments_refresh", "events_fetch", "login_reminder", "gap_check",
                     "scan", "thesis_exit", "eod", "archive",
                     "no_entry_start", "no_entry_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"expected HH:MM (24h IST), got {v!r}")
        return v

    @model_validator(mode="after")
    def _window(self) -> "ScheduleCfg":
        if self.no_entry_start >= self.no_entry_end:
            raise ValueError("schedule.no_entry_start must be before schedule.no_entry_end")
        return self


class KillSwitchCfg(_Section):
    stale_quote_secs: PosInt = 60
    max_rejections_per_day: NonNegInt = 3
    reconcile_every_min: PosInt = 5


class CostsCfg(_Section):
    # "zerodha": the Indian stack below (brokerage, STT, exchange, SEBI, stamp, GST).
    # "per_contract": a flat fee per option contract plus brokerage_per_order (US brokers).
    model: Literal["zerodha", "per_contract"] = "zerodha"
    per_contract: NonNegFloat = 0.0
    brokerage_per_order: NonNegFloat = 20
    stt_sell_premium: NonNegFloat = 0.0015
    # NSE options transaction charge on premium (Zerodha charges page, Nov 2026: 0.03503%).
    exchange_txn_pct: Optional[NonNegFloat] = 0.0003503
    sebi_fee_per_crore: NonNegFloat = 10
    stamp_buy_pct: NonNegFloat = 0.00003
    gst: NonNegFloat = 0.18

    @property
    def missing(self) -> List[str]:
        """Cost rates that are still unset. The cost model refuses to price trades until empty."""
        return [name for name, val in self.model_dump().items() if val is None]


class MarketCfg(_Section):
    name: Literal["nse", "us"] = "nse"
    currency: Literal["INR", "USD"] = "INR"     # display only


class OpsCfg(_Section):
    backup_keep: NonNegInt = 14         # daily database copies kept; 0 = no backups
    heartbeat: bool = True              # end-of-day summary alert (silence means the system is down)
    record_market: bool = True          # save each scan's quotes to paths.market_store for backtests


class ArchiveCfg(_Section):
    """Kite history archiver (app/data/history_archiver.py)."""
    enabled: bool = True
    band: Annotated[float, Field(gt=0, le=1)] = 0.25       # option strikes within +-25% of the stock price
    max_expiries: PosInt = 3                                # listed monthly series to backfill
    days_before_expiry: PosInt = 3                          # top up a series in its last N trading days
    max_requests_per_run: PosInt = 5000                     # ~28 minutes at Kite's 3 requests a second
    equity_days: Annotated[int, Field(ge=30, le=2000)] = 2000


class PathsCfg(_Section):
    db: str = "data/trading.db"
    logs: str = "logs"
    levels_manual: str = "levels_manual.csv"
    events_override: str = "events_override.csv"
    sectors_manual: str = "sectors_manual.csv"
    holidays_manual: str = "holidays_manual.csv"
    kite_session: str = "data/kite_session.json"
    imports: str = "imports"                   # imports/events/*.csv: event exports from nseindia.com
    market_store: str = "data/market_nse.db"   # recorded NSE quotes for backtests
    fixtures: str = "fixtures"


class Config(_Section):
    mode: Literal["paper", "live"] = "paper"
    capital: Annotated[float, Field(gt=0)] = 500_000
    universe: UniverseCfg = UniverseCfg()
    dte_min: NonNegInt = 15
    dte_max: PosInt = 35
    events: EventsCfg = EventsCfg()
    iv: IVCfg = IVCfg()
    levels: LevelsCfg = LevelsCfg()
    direction: DirectionCfg = DirectionCfg()
    strikes: StrikesCfg = StrikesCfg()
    spread: SpreadCfg = SpreadCfg()
    ev: EVCfg = EVCfg()
    liquidity: LiquidityCfg = LiquidityCfg()
    analytics: AnalyticsCfg = AnalyticsCfg()
    broker: BrokerCfg = BrokerCfg()
    scan: ScanCfg = ScanCfg()
    monitor: MonitorCfg = MonitorCfg()
    reporting: ReportingCfg = ReportingCfg()
    exit: ExitCfg = ExitCfg()
    risk: RiskCfg = RiskCfg()
    paper: PaperCfg = PaperCfg()
    live: LiveCfg = LiveCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    killswitch: KillSwitchCfg = KillSwitchCfg()
    costs: CostsCfg = CostsCfg()
    market: MarketCfg = MarketCfg()
    archive: ArchiveCfg = ArchiveCfg()
    ops: OpsCfg = OpsCfg()
    paths: PathsCfg = PathsCfg()

    # Directory the config file was loaded from; relative paths resolve against it.
    base_dir: Path = Field(default=PROJECT_ROOT, exclude=True)

    @model_validator(mode="after")
    def _dte(self) -> "Config":
        if self.dte_min >= self.dte_max:
            raise ValueError("dte_min must be below dte_max")
        if self.exit.days_before_expiry >= self.dte_min:
            raise ValueError("exit.days_before_expiry must be below dte_min, or every trade "
                             "would hit the time stop on entry")
        return self

    def strategy_hash(self) -> str:
        """Short hash of everything that can change a trading decision (paths and ops excluded)."""
        import hashlib
        import json

        data = self.model_dump(mode="json", exclude={"base_dir", "paths", "ops"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def db_path(self) -> Path:
        return self.resolve(self.paths.db)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.paths.logs)


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


def load_config(path: Optional[Path | str] = None) -> Config:
    """Load and validate config.yaml. Raises ConfigError with a readable message on failure."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    if "base_dir" in raw:
        raise ConfigError("base_dir is not a config key")
    try:
        return Config(**raw, base_dir=path.resolve().parent)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{_format_errors(exc)}") from exc
