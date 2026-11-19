"""ORM for the platform database. The SQL migrations in platform_core/migrations own the schema;
these classes mirror it (a test checks they match)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PBase(DeclarativeBase):
    pass


class Account(PBase):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy: Mapped[str] = mapped_column(String)
    opening_capital: Mapped[float] = mapped_column(Float)
    start_date: Mapped[Optional[dt.date]] = mapped_column(Date)


class PTrade(PBase):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    symbol: Mapped[str] = mapped_column(String)
    sector: Mapped[Optional[str]] = mapped_column(String)
    setup_tag: Mapped[Optional[str]] = mapped_column(String)
    setup_id: Mapped[Optional[int]] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String)
    legs_json: Mapped[str] = mapped_column(Text)
    expiry: Mapped[dt.date] = mapped_column(Date)
    lots: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer)
    credit: Mapped[float] = mapped_column(Float)
    entry_charges: Mapped[float] = mapped_column(Float, default=0.0)
    margin: Mapped[Optional[float]] = mapped_column(Float)
    rom: Mapped[Optional[float]] = mapped_column(Float)
    level: Mapped[Optional[float]] = mapped_column(Float)
    loss_at_kill: Mapped[Optional[float]] = mapped_column(Float)
    one_r: Mapped[Optional[float]] = mapped_column(Float)
    max_loss: Mapped[float] = mapped_column(Float)
    entry_ts: Mapped[dt.datetime] = mapped_column(DateTime)
    exit_ts: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    exit_reason: Mapped[Optional[str]] = mapped_column(String)
    exit_cost: Mapped[Optional[float]] = mapped_column(Float)
    exit_charges: Mapped[Optional[float]] = mapped_column(Float)
    pnl_inr: Mapped[Optional[float]] = mapped_column(Float)
    pnl_r: Mapped[Optional[float]] = mapped_column(Float)
    holding_days: Mapped[Optional[int]] = mapped_column(Integer)
    book: Mapped[Optional[str]] = mapped_column(String)
    failed_filters: Mapped[Optional[str]] = mapped_column(Text)
    nifty_trend: Mapped[Optional[str]] = mapped_column(String)
    owner_stop: Mapped[Optional[float]] = mapped_column(Float)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    context_json: Mapped[Optional[str]] = mapped_column(Text)


class POrder(PBase):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    trade_id: Mapped[Optional[str]] = mapped_column(String)
    purpose: Mapped[str] = mapped_column(String)
    legs_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)


class LedgerEntry(PBase):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    entry_type: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)
    cash_balance: Mapped[float] = mapped_column(Float)
    used_margin: Mapped[float] = mapped_column(Float)
    trade_id: Mapped[Optional[str]] = mapped_column(String)
    note: Mapped[Optional[str]] = mapped_column(Text)


class PMark(PBase):
    __tablename__ = "marks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"))
    spot: Mapped[Optional[float]] = mapped_column(Float)
    legs_json: Mapped[Optional[str]] = mapped_column(Text)
    cost_to_close: Mapped[Optional[float]] = mapped_column(Float)


class LevelRow(PBase):
    __tablename__ = "levels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String)
    setup: Mapped[int] = mapped_column(Integer)
    level: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String)
    day0_date: Mapped[dt.date] = mapped_column(Date)
    volume_mult: Mapped[Optional[float]] = mapped_column(Float)
    vwap_dist_atr: Mapped[Optional[float]] = mapped_column(Float)
    hvn_dist_atr: Mapped[Optional[float]] = mapped_column(Float)
    breakout_low: Mapped[Optional[float]] = mapped_column(Float)
    setup_id: Mapped[Optional[int]] = mapped_column(Integer)


class SetupRow(PBase):
    __tablename__ = "setups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String)
    setup: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    level: Mapped[float] = mapped_column(Float)
    day0_date: Mapped[dt.date] = mapped_column(Date)
    route: Mapped[Optional[str]] = mapped_column(String)
    window_start: Mapped[Optional[dt.date]] = mapped_column(Date)
    expiry: Mapped[Optional[dt.date]] = mapped_column(Date)
    zone_json: Mapped[Optional[str]] = mapped_column(Text)
    day0_oi_json: Mapped[Optional[str]] = mapped_column(Text)
    zone_oi_share: Mapped[Optional[float]] = mapped_column(Float)
    local_pcr: Mapped[Optional[float]] = mapped_column(Float)
    sessions_json: Mapped[Optional[str]] = mapped_column(Text)
    volume_mult: Mapped[Optional[float]] = mapped_column(Float)
    failed_filters: Mapped[Optional[str]] = mapped_column(Text)
    signals_json: Mapped[Optional[str]] = mapped_column(Text)
    strict_trade_id: Mapped[Optional[str]] = mapped_column(String)
    research_trade_id: Mapped[Optional[str]] = mapped_column(String)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ScreenerRow(PBase):
    __tablename__ = "screener_daily"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    event_flag: Mapped[Optional[bool]] = mapped_column(Boolean)
    liquidity_flag: Mapped[Optional[bool]] = mapped_column(Boolean)
    rom_call: Mapped[Optional[float]] = mapped_column(Float)
    rom_put: Mapped[Optional[float]] = mapped_column(Float)
    rank: Mapped[Optional[int]] = mapped_column(Integer)
    detail_json: Mapped[Optional[str]] = mapped_column(Text)


class CloseCheck(PBase):
    __tablename__ = "close_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String)
    ref: Mapped[Optional[str]] = mapped_column(String)
    symbol: Mapped[Optional[str]] = mapped_column(String)
    snapshot_value: Mapped[Optional[float]] = mapped_column(Float)
    official_value: Mapped[Optional[float]] = mapped_column(Float)
    mismatch: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[Optional[str]] = mapped_column(Text)


class Upload(PBase):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    rows: Mapped[Optional[int]] = mapped_column(Integer)


class PlatformState(PBase):
    __tablename__ = "platform_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
