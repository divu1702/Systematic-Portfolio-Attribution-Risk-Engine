"""SQLAlchemy ORM models.

The schema itself is owned by the SQL files in app/data/migrations/. These models mirror it
for convenient reads and writes; tests/test_db.py fails if the two drift apart.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    instrument_token: Mapped[Optional[int]] = mapped_column(Integer)
    exchange: Mapped[str] = mapped_column(String)
    tradingsymbol: Mapped[str] = mapped_column(String)
    underlying: Mapped[str] = mapped_column(String)
    instrument_type: Mapped[str] = mapped_column(String)
    expiry: Mapped[Optional[dt.date]] = mapped_column(Date)
    strike: Mapped[Optional[float]] = mapped_column(Float)
    lot_size: Mapped[int] = mapped_column(Integer)
    tick_size: Mapped[float] = mapped_column(Float)


class UnderlyingDaily(Base):
    __tablename__ = "underlying_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[Optional[int]] = mapped_column(Integer)
    atr14: Mapped[Optional[float]] = mapped_column(Float)
    hv20: Mapped[Optional[float]] = mapped_column(Float)
    hv_pctile_252: Mapped[Optional[float]] = mapped_column(Float)


class IVSnapshot(Base):
    __tablename__ = "iv_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    date: Mapped[dt.date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String)
    expiry: Mapped[dt.date] = mapped_column(Date)
    fut_price: Mapped[float] = mapped_column(Float)
    atm_iv: Mapped[float] = mapped_column(Float)
    iv_25d_put: Mapped[Optional[float]] = mapped_column(Float)
    iv_25d_call: Mapped[Optional[float]] = mapped_column(Float)
    ivp_252: Mapped[Optional[float]] = mapped_column(Float)
    method: Mapped[Optional[str]] = mapped_column(String)


class Level(Base):
    __tablename__ = "levels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    touches: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    event_date: Mapped[dt.date] = mapped_column(Date)
    event_type: Mapped[str] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    scan_id: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[Optional[str]] = mapped_column(String)
    short_k: Mapped[Optional[float]] = mapped_column(Float)
    long_k: Mapped[Optional[float]] = mapped_column(Float)
    expiry: Mapped[Optional[dt.date]] = mapped_column(Date)
    dte: Mapped[Optional[int]] = mapped_column(Integer)
    lot_size: Mapped[Optional[int]] = mapped_column(Integer)
    credit_nat: Mapped[Optional[float]] = mapped_column(Float)
    width: Mapped[Optional[float]] = mapped_column(Float)
    max_loss: Mapped[Optional[float]] = mapped_column(Float)
    p_market: Mapped[Optional[float]] = mapped_column(Float)
    p_model: Mapped[Optional[float]] = mapped_column(Float)
    ev: Mapped[Optional[float]] = mapped_column(Float)
    ev_ratio: Mapped[Optional[float]] = mapped_column(Float)
    f1_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f2_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f3_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f4_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f5_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f6_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f7_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f8_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    f9_pass: Mapped[Optional[bool]] = mapped_column(Boolean)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text)
    inputs_json: Mapped[Optional[str]] = mapped_column(Text)


class Trade(Base):
    __tablename__ = "trades"
    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    candidate_id: Mapped[Optional[int]] = mapped_column(ForeignKey("candidates.id"))
    symbol: Mapped[str] = mapped_column(String)
    sector: Mapped[Optional[str]] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    expiry: Mapped[dt.date] = mapped_column(Date)
    legs_json: Mapped[str] = mapped_column(Text)
    lots: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer)
    entry_ts: Mapped[dt.datetime] = mapped_column(DateTime)
    entry_credit: Mapped[float] = mapped_column(Float)
    costs: Mapped[float] = mapped_column(Float, default=0.0)
    max_loss: Mapped[float] = mapped_column(Float)
    margin: Mapped[Optional[float]] = mapped_column(Float)
    margin_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    iv_at_entry: Mapped[Optional[float]] = mapped_column(Float)
    hv20: Mapped[Optional[float]] = mapped_column(Float)
    ivp: Mapped[Optional[float]] = mapped_column(Float)
    ivp_method: Mapped[Optional[str]] = mapped_column(String)
    level_ref: Mapped[Optional[float]] = mapped_column(Float)
    exit_ts: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    exit_cost: Mapped[Optional[float]] = mapped_column(Float)
    exit_costs: Mapped[Optional[float]] = mapped_column(Float)
    exit_reason: Mapped[Optional[str]] = mapped_column(String)
    pnl_gross: Mapped[Optional[float]] = mapped_column(Float)
    pnl_net: Mapped[Optional[float]] = mapped_column(Float)
    mae: Mapped[Optional[float]] = mapped_column(Float)
    mfe: Mapped[Optional[float]] = mapped_column(Float)
    gap_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    p_market: Mapped[Optional[float]] = mapped_column(Float)
    p_model: Mapped[Optional[float]] = mapped_column(Float)
    breakeven_wr: Mapped[Optional[float]] = mapped_column(Float)
    iv_hv: Mapped[Optional[float]] = mapped_column(Float)
    short_k: Mapped[Optional[float]] = mapped_column(Float)
    long_k: Mapped[Optional[float]] = mapped_column(Float)
    replayed: Mapped[bool] = mapped_column(Boolean, default=False)


class Mark(Base):
    __tablename__ = "marks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.trade_id"))
    spot: Mapped[Optional[float]] = mapped_column(Float)
    legs_json: Mapped[Optional[str]] = mapped_column(Text)
    cost_to_close: Mapped[Optional[float]] = mapped_column(Float)
    unrealised_pnl: Mapped[Optional[float]] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String, default="live")


class Order(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    trade_id: Mapped[Optional[str]] = mapped_column(ForeignKey("trades.trade_id"))
    purpose: Mapped[str] = mapped_column(String)
    leg: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    qty: Mapped[int] = mapped_column(Integer)
    limit_px: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)
    fill_px: Mapped[Optional[float]] = mapped_column(Float)
    broker_ref: Mapped[Optional[str]] = mapped_column(String)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)


class Equity(Base):
    __tablename__ = "equity"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    equity: Mapped[float] = mapped_column(Float)
    realised: Mapped[float] = mapped_column(Float)
    unrealised: Mapped[float] = mapped_column(Float)
    margin_used: Mapped[float] = mapped_column(Float)
    peak: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)


class EventFetch(Base):
    __tablename__ = "event_fetches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String)
    ok: Mapped[bool] = mapped_column(Boolean)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)


class Sector(Base):
    __tablename__ = "sectors"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    sector: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Holiday(Base):
    __tablename__ = "holidays"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String)


class OptionTurnover(Base):
    __tablename__ = "option_turnover"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String)
    turnover: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String)


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="pending")
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
