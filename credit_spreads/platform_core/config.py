"""Pydantic-validated loader for platform.yaml (Level-Based Credit Spreads spec).

Every number the platform uses lives in platform.yaml. Unknown keys are rejected so a typo fails
loudly. The values are the owner's; change them only with the owner's approval.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Annotated, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / "platform.yaml"

Fraction = Annotated[float, Field(gt=0, lt=1)]
PosInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]
PosFloat = Annotated[float, Field(gt=0)]
NonNegFloat = Annotated[float, Field(ge=0)]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ACCOUNTS = ("oi_tight", "oi_loose", "level_strict", "level_research", "discretionary")


class PlatformConfigError(Exception):
    """platform.yaml is missing, unreadable or invalid."""


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hhmm(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"expected HH:MM (24h IST), got {v!r}")
    return v


class ControlCentreCfg(_S):
    access: Literal["tailscale"] = "tailscale"
    pin_required: bool = True
    refresh_seconds: PosInt = 30
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1024, le=65535)] = 8765


class SwingCfg(_S):
    side_bars: PosInt
    lookback: Optional[PosInt] = None


class SwingsCfg(_S):
    major: SwingCfg = SwingCfg(side_bars=10, lookback=250)
    minor: SwingCfg = SwingCfg(side_bars=5)


class S1Cfg(_S):
    min_vol_mult: PosFloat = 2.0


class S2Cfg(_S):
    min_vol_mult: PosFloat = 2.0
    armed_sessions: PosInt = 20
    retest_atr: PosFloat = 0.5
    acceptance_closes: PosInt = 3


class S3Cfg(_S):
    min_vol_mult: PosFloat = 1.0
    min_pullback_sessions: PosInt = 2


class S4Cfg(_S):
    min_vol_mult: PosFloat = 1.0
    cluster_atr: PosFloat = 0.5
    lookback: PosInt = 60
    min_height_atr: PosFloat = 3.0
    touch_atr: PosFloat = 0.25
    day0_within_sessions: NonNegInt = 3


class SetupsCfg(_S):
    s1_failed_extension: S1Cfg = S1Cfg()
    s2_breakout_hold: S2Cfg = S2Cfg()
    s3_trend_pullback: S3Cfg = S3Cfg()
    s4_range_rejection: S4Cfg = S4Cfg()


class ConfirmationCfg(_S):
    window_sessions: PosInt = 2
    zone_strikes_per_side: PosInt = 3
    oi_tolerance: Fraction = 0.10


class StrikesCfg(_S):
    min_rom: Fraction = 0.10
    width_atr: PosFloat = 1.5


class LiquidityCfg(_S):
    min_oi_value_inr: PosFloat = 30_000_000
    max_spread_pct: Fraction = 0.05


class SizingCfg(_S):
    risk_at_stop: Fraction = 0.05
    iv_cushion_points: NonNegFloat = 5
    gap_cap_position: Fraction = 0.15
    gap_cap_portfolio: Fraction = 0.25
    max_positions: PosInt = 3
    one_per_sector: bool = True


class ExpiryCfg(_S):
    min_dte_current_month: PosInt = 15


class ExitsCfg(_S):
    profit_target_capture: Fraction = 0.70
    time_stop_trading_days_before_expiry: PosInt = 5
    event_exit: bool = True


class ScreenerCfg(_S):
    run_at: str = "15:20"
    report_top_n: PosInt = 20
    event_staleness_days: PosInt = 2

    @field_validator("run_at")
    @classmethod
    def _time(cls, v: str) -> str:
        return _hhmm(v)


class StrengthCfg(_S):
    hvn_lookback: PosInt = 60
    hvn_source: Literal["1minute"] = "1minute"
    hvn_bucket_atr: PosFloat = 0.1


class PaperCfg(_S):
    extra_slip_ticks: NonNegInt = 1


class JournalCfg(_S):
    match_window_sessions: NonNegInt = 2
    default_1r: Literal["max_loss"] = "max_loss"


class BooksCfg(_S):
    research_enabled: bool = True
    research_exempt_from_portfolio_limits: bool = True


class MarketFilterCfg(_S):
    index: str = "NIFTY 50"
    swing_side_bars: PosInt = 5
    mode: Literal["log_only"] = "log_only"


class ReviewCfg(_S):
    min_strict_trades_before_changes: PosInt = 30
    min_trades_per_filter_group: PosInt = 30


class CostsCfg(_S):
    brokerage_per_order: NonNegFloat = 20
    stt_sell_premium: NonNegFloat = 0.0015
    exchange_txn_pct: NonNegFloat = 0.0003553
    sebi_fee_per_crore: NonNegFloat = 10
    stamp_buy_pct: NonNegFloat = 0.00003
    gst: NonNegFloat = 0.18


class SettlementCfg(_S):
    stt_exercise: NonNegFloat = 0.0015
    physical_delivery_brokerage_pct: NonNegFloat = 0.0025
    delivery_margin_steps: List[Annotated[float, Field(ge=0, le=1)]] = Field(
        default_factory=lambda: [0.10, 0.25, 0.45, 0.70], min_length=1)
    delivery_margin_full_pct: Annotated[float, Field(ge=0, le=1)] = 0.20


class ScheduleCfg(_S):
    login_link: str = "08:45"
    events_reminder: str = "08:45"
    gap_check: str = "09:30"
    exit_check_every_min: PosInt = 30
    official_recheck: str = "18:30"

    @field_validator("login_link", "events_reminder", "gap_check", "official_recheck")
    @classmethod
    def _times(cls, v: str) -> str:
        return _hhmm(v)

    @field_validator("exit_check_every_min")
    @classmethod
    def _grid(cls, v: int) -> int:
        if 60 % v and v % 60:
            raise ValueError("schedule.exit_check_every_min must divide 60 or be a multiple of it")
        return v


class DataCfg(_S):
    events_file_max_age_days: PosInt = 1
    sectors_file_fresh_days: PosInt = 90


class OISystemCfg(_S):
    config: str = "config.yaml"
    books: Dict[str, str] = Field(default_factory=lambda: {"oi_tight": "main", "oi_loose": "v2"})

    @field_validator("books")
    @classmethod
    def _accounts(cls, v: Dict[str, str]) -> Dict[str, str]:
        if set(v) != {"oi_tight", "oi_loose"}:
            raise ValueError("oi_system.books must map exactly oi_tight and oi_loose to the earlier system's books")
        return v


class PathsCfg(_S):
    db: str = "data/platform.db"
    chain_snap: str = "data/chain_snap"
    reports: str = "reports/platform"
    imports: str = "imports"
    sectors_file: str = "sectors.csv"
    fixtures: str = "tests/fixtures/realdays"


class PlatformConfig(_S):
    go_live_date: Optional[date] = None
    capital: PosFloat = 500_000
    accounts: List[str] = Field(default_factory=lambda: list(ACCOUNTS))
    control_centre: ControlCentreCfg = ControlCentreCfg()
    atr_period: PosInt = 14
    volume_avg_sessions: PosInt = 20
    swings: SwingsCfg = SwingsCfg()
    setups: SetupsCfg = SetupsCfg()
    confirmation: ConfirmationCfg = ConfirmationCfg()
    strikes: StrikesCfg = StrikesCfg()
    liquidity: LiquidityCfg = LiquidityCfg()
    sizing: SizingCfg = SizingCfg()
    expiry: ExpiryCfg = ExpiryCfg()
    exits: ExitsCfg = ExitsCfg()
    screener: ScreenerCfg = ScreenerCfg()
    strength_signals: StrengthCfg = StrengthCfg()
    paper: PaperCfg = PaperCfg()
    journal: JournalCfg = JournalCfg()
    books: BooksCfg = BooksCfg()
    market_filter: MarketFilterCfg = MarketFilterCfg()
    review: ReviewCfg = ReviewCfg()
    costs: CostsCfg = CostsCfg()
    settlement: SettlementCfg = SettlementCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    data: DataCfg = DataCfg()
    oi_system: OISystemCfg = OISystemCfg()
    paths: PathsCfg = PathsCfg()
    base_dir: Path = Field(default=PROJECT_ROOT, exclude=True)

    @field_validator("accounts")
    @classmethod
    def _five(cls, v: List[str]) -> List[str]:
        if sorted(v) != sorted(ACCOUNTS) or len(v) != len(ACCOUNTS):
            raise ValueError(f"accounts must be exactly {', '.join(ACCOUNTS)}")
        return v

    @model_validator(mode="after")
    def _swings(self) -> "PlatformConfig":
        if not self.swings.major.lookback:
            raise ValueError("swings.major.lookback is required")
        return self

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def db_path(self) -> Path:
        return self.resolve(self.paths.db)

    def is_live(self, d: date) -> bool:
        """True once go_live_date is set and reached: all five accounts trade from then on."""
        return self.go_live_date is not None and d >= self.go_live_date


def _errors(err: ValidationError) -> str:
    return "\n".join(f"  {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in err.errors())


def load_platform_config(path: Optional[Path | str] = None) -> PlatformConfig:
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        raise PlatformConfigError(f"platform config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PlatformConfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlatformConfigError(f"{path} must contain a mapping at the top level")
    try:
        return PlatformConfig(**raw, base_dir=path.resolve().parent)
    except ValidationError as exc:
        raise PlatformConfigError(f"invalid config in {path}:\n{_errors(exc)}") from exc
