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
    top_n: PosInt = 50
    turnover_window: PosInt = 20


class EventsCfg(_Section):
    buffer_days: NonNegInt = 1
    max_staleness_days: NonNegInt = 2


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


class LiquidityCfg(_Section):
    max_spread_pct: Fraction = 0.05
    min_oi_lots: NonNegInt = 500
    min_vol_lots: NonNegInt = 50


class AnalyticsCfg(_Section):
    hv_window: PosInt = 20
    atr_window: PosInt = 14
    trading_days_per_year: PosInt = 252


class ExitCfg(_Section):
    stop_mult: Annotated[float, Field(gt=1)] = 2.0
    tp_frac: Fraction = 0.50
    days_before_expiry: NonNegInt = 5
    thesis_stop: bool = True
    on_new_event: bool = True


class RiskCfg(_Section):
    per_trade: Fraction = 0.02
    max_open: PosInt = 5
    max_same_direction: PosInt = 3
    max_total: Fraction = 0.08
    max_per_sector: PosInt = 1
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

    @field_validator("require_approval")
    @classmethod
    def _v1_semi_auto(cls, v: bool) -> bool:
        if not v:
            raise ValueError("live.require_approval must be true: live entries are semi-automatic in v1")
        return v


class ScheduleCfg(_Section):
    login_reminder: str = "09:00"
    gap_check: str = "09:20"
    scan: str = "15:00"
    thesis_exit: str = "15:15"
    eod: str = "15:35"
    monitor_every_min: PosInt = 5
    no_entry_start: str = "09:15"
    no_entry_end: str = "09:20"

    @field_validator("login_reminder", "gap_check", "scan", "thesis_exit", "eod",
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
    brokerage_per_order: NonNegFloat = 20
    stt_sell_premium: NonNegFloat = 0.0015
    # Deliberately no default: the owner must supply the current NSE rate.
    exchange_txn_pct: Optional[NonNegFloat] = None
    sebi_fee_per_crore: NonNegFloat = 10
    stamp_buy_pct: NonNegFloat = 0.00003
    gst: NonNegFloat = 0.18

    @property
    def missing(self) -> List[str]:
        """Cost rates that are still unset. The cost model refuses to price trades until empty."""
        return [name for name, val in self.model_dump().items() if val is None]


class PathsCfg(_Section):
    db: str = "data/trading.db"
    logs: str = "logs"
    levels_manual: str = "levels_manual.csv"
    events_override: str = "events_override.csv"


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
    exit: ExitCfg = ExitCfg()
    risk: RiskCfg = RiskCfg()
    paper: PaperCfg = PaperCfg()
    live: LiveCfg = LiveCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    killswitch: KillSwitchCfg = KillSwitchCfg()
    costs: CostsCfg = CostsCfg()
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
