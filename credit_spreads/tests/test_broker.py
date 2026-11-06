import io
import json
import threading
import urllib.request
from datetime import date, datetime, timedelta

import pytest

from app.broker.base import RateLimiter, SessionInvalid, parse_quote
from app.broker.kite import KiteBroker, extract_request_token, session_start_boundary
from app.broker.login import wait_for_redirect
from app.broker.mock import FixtureBroker
from app.broker.synthetic import SyntheticMarket, last_tuesday, strike_step
from app.clock import SimClock
from app.data import instruments as inst
from app import market
from app.recorder import record_fixtures
from app.views import option_chain_report
from tests.conftest import SIM_NOW, shared_market


# ----------------------------------------------------------------------------- helpers

def test_extract_request_token():
    url = "http://127.0.0.1:5000/kite/callback?request_token=abc123XYZ&action=login&status=success"
    assert extract_request_token(url) == "abc123XYZ"
    assert extract_request_token("  abc123XYZ ") == "abc123XYZ"
    with pytest.raises(ValueError, match="did not succeed"):
        extract_request_token("http://x/cb?request_token=abc&status=error")
    with pytest.raises(ValueError):
        extract_request_token("http://x/cb?action=login")
    with pytest.raises(ValueError):
        extract_request_token("not a token")


def test_session_boundary_is_six_am():
    assert session_start_boundary(datetime(2026, 11, 6, 9, 0)) == datetime(2026, 11, 6, 6, 0)
    assert session_start_boundary(datetime(2026, 11, 6, 5, 0)) == datetime(2026, 11, 5, 6, 0)


def test_rate_limiter_spaces_calls():
    t = {"now": 0.0}
    slept = []
    rl = RateLimiter(2.0, sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)),
                     monotonic=lambda: t["now"])
    for _ in range(3):
        rl.wait()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]


def test_parse_quote_drops_empty_levels():
    q = parse_quote("NFO:X", {"last_price": 10, "volume": 500, "oi": 1000,
                              "depth": {"buy": [{"price": 9.9, "quantity": 50}, {"price": 0, "quantity": 0}],
                                        "sell": [{"price": 10.1, "quantity": 25}]}})
    assert (q.bid, q.ask, q.mid) == (9.9, 10.1, 10.0)
    assert len(q.bids) == 1 and q.spread_pct == pytest.approx(0.02)
    empty = parse_quote("NFO:Y", {"last_price": 5, "depth": {"buy": [], "sell": []}})
    assert empty.bid == 0 and empty.mid is None


# ----------------------------------------------------------------------------- KiteBroker

class FakeKite:
    def __init__(self):
        self.access_token = None
        self.quote_calls = []
        self.hist_calls = []

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?v=3&api_key=k"

    def generate_session(self, request_token, api_secret):
        assert api_secret == "s"
        return {"access_token": f"tok-{request_token}", "user_id": "AB1234"}

    def set_access_token(self, t):
        self.access_token = t

    def instruments(self, exchange):
        return [{"tradingsymbol": "X", "expiry": datetime(2026, 11, 24), "instrument_type": "CE"},
                {"tradingsymbol": "Y", "expiry": "", "instrument_type": "EQ"}]

    def quote(self, keys):
        self.quote_calls.append(list(keys))
        return {k: {"last_price": 1.0, "timestamp": datetime(2026, 11, 6, 15, 0)} for k in keys}

    def historical_data(self, token, start, end, interval, oi=False):
        self.hist_calls.append((start, end))
        out, d = [], start
        while d <= end:
            out.append({"date": d, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0})
            d += timedelta(days=1)
        return out

    def basket_order_margins(self, orders, consider_positions=True, mode=None):
        return {"initial": {"total": 120000.0}, "final": {"total": 45000.5}}

    def positions(self):
        return {"net": [{"tradingsymbol": "X", "quantity": -1350}], "day": []}


@pytest.fixture
def kite_broker(cfg, tmp_path):
    clock = SimClock(datetime(2026, 11, 6, 9, 0))
    b = KiteBroker(cfg, {"KITE_API_KEY": "k", "KITE_API_SECRET": "s"}, clock, kite=FakeKite())
    b.session_path = tmp_path / "session.json"
    return b, clock


def test_kite_requires_session_then_persists_for_the_day(kite_broker):
    b, clock = kite_broker
    assert not b.session_valid()
    with pytest.raises(SessionInvalid):
        b.quote_raw(["NFO:X"])
    b.complete_login("http://127.0.0.1:5000/kite/callback?request_token=RT1&status=success")
    assert b.kite.access_token == "tok-RT1"
    saved = json.loads(b.session_path.read_text())
    assert saved["user_id"] == "AB1234"
    clock.set(datetime(2026, 11, 6, 15, 30))
    assert b.session_valid()
    clock.set(datetime(2026, 11, 7, 6, 1))          # next morning: token expired
    assert not b.session_valid()


def test_kite_quotes_are_batched(kite_broker, cfg):
    b, _ = kite_broker
    b.complete_login("RT1")
    b.cfg = cfg.model_copy(update={"broker": cfg.broker.model_copy(update={"quote_batch": 2, "quote_rps": 1.0})})
    b._quote_rl = RateLimiter(1000)
    out = b.quote_raw(["NFO:A", "NFO:B", "NFO:C"])
    assert [len(c) for c in b.kite.quote_calls] == [2, 1]
    assert set(out) == {"NFO:A", "NFO:B", "NFO:C"}


def test_kite_historical_chunks_and_dedupes(kite_broker):
    b, _ = kite_broker
    b.complete_login("RT1")
    b._hist_rl = RateLimiter(1000)
    rows = b.historical(1, datetime(2026, 1, 1), datetime(2026, 9, 1), "5minute")
    assert len(b.kite.hist_calls) == 3                       # 100-day chunks
    dates = [r["date"] for r in rows]
    assert len(dates) == len(set(dates))


def test_kite_margin_positions_instruments(kite_broker):
    b, _ = kite_broker
    b.complete_login("RT1")
    assert b.basket_margin([{}]) == 45000.5
    assert b.positions()[0]["quantity"] == -1350
    rows = b.instruments("NFO")
    assert rows[0]["expiry"] == date(2026, 11, 24) and rows[1]["expiry"] is None


# ----------------------------------------------------------------------------- login server

def test_wait_for_redirect_catches_request_token():
    url = "http://127.0.0.1:5765/kite/callback"
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("url", wait_for_redirect(url, timeout=10)))
    t.start()
    for _ in range(50):
        try:
            body = urllib.request.urlopen(f"{url}?request_token=RTX&action=login&status=success", timeout=1).read()
            break
        except OSError:
            threading.Event().wait(0.1)
    t.join(5)
    assert b"Login received" in body
    assert extract_request_token(result["url"]) == "RTX"


# ----------------------------------------------------------------------------- synthetic market

def test_synthetic_market_is_deterministic():
    a = SyntheticMarket(seed=3, n_symbols=3, anchor=date(2026, 11, 6), history_days=300, future_days=30)
    b = SyntheticMarket(seed=3, n_symbols=3, anchor=date(2026, 11, 6), history_days=300, future_days=30)
    ts = datetime(2026, 11, 6, 11, 0)
    assert a.spot("STK01", ts) == b.spot("STK01", ts)
    k = a.instruments("NFO", ts.date())[5]["tradingsymbol"]
    assert a.quote(f"NFO:{k}", ts) == b.quote(f"NFO:{k}", ts)


def test_synthetic_calendar_and_expiries():
    m = shared_market()
    assert all(d.weekday() < 5 for d in m.days)
    assert date(2026, 1, 26) not in m.day_index           # Republic Day
    assert last_tuesday(2026, 11) == date(2026, 11, 24)
    assert date(2026, 11, 24) in m.expiries
    assert strike_step(584) == 10 and strike_step(2200) == 20 and strike_step(240) == 2.5


def test_mock_never_serves_the_future(mock_ctx):
    b, clock = mock_ctx.broker, mock_ctx.clock
    tok = b.market.info("NSE:STK01").token
    rows = b.historical(tok, SIM_NOW - timedelta(days=30), SIM_NOW + timedelta(days=30), "day")
    assert rows[-1]["date"].date() == SIM_NOW.date()
    five = b.historical(tok, SIM_NOW.replace(hour=9), SIM_NOW + timedelta(days=3), "5minute")
    assert max(r["date"] for r in five) + timedelta(minutes=5) <= SIM_NOW
    # the partial daily candle today matches the current price
    assert rows[-1]["close"] == pytest.approx(b.market.spot("STK01", SIM_NOW), abs=0.01)


def test_mock_quote_shape_and_expired_contracts(mock_ctx):
    rows = mock_ctx.broker.instruments("NFO")
    opt = next(r for r in rows if r["instrument_type"] == "PE" and r["name"] == "STK02")
    key = f"NFO:{opt['tradingsymbol']}"
    q = mock_ctx.broker.quotes([key])[key]
    assert len(q.asks) == 5 and q.ask > q.bid >= 0
    assert all(lv.quantity % opt["lot_size"] == 0 for lv in q.asks)
    assert mock_ctx.broker.quotes(["NFO:STK0225OCT100PE"]) == {}     # expired before SIM_NOW


# ----------------------------------------------------------------------------- instrument master

def test_refresh_excludes_index_options_and_reads_lots(mock_ctx):
    res = inst.refresh_instruments(mock_ctx.engine, mock_ctx.broker, SIM_NOW.date())
    stocks = inst.fno_stocks(mock_ctx.engine, SIM_NOW.date())
    assert res.excluded_underlyings == 1 and "SYNIDX" not in stocks
    assert len(stocks) == 8
    exps = inst.expiries(mock_ctx.engine, SIM_NOW.date(), "STK01")
    assert exps[0] == date(2026, 11, 24) and len(exps) == 3
    ch = inst.load_chain(mock_ctx.engine, SIM_NOW.date(), "STK01", exps[0])
    spec = mock_ctx.broker.market.spec("STK01")
    assert ch.lot_size == spec.lot and ch.future is not None and ch.equity_token
    assert ch.strike_step == strike_step(mock_ctx.broker.market.paths["STK01"][
        mock_ctx.broker.market.day_index[exps[0]] - 63, 0])
    # refreshing again replaces, not duplicates
    assert inst.refresh_instruments(mock_ctx.engine, mock_ctx.broker, SIM_NOW.date()).rows == res.rows


def test_strike_step_inference():
    assert inst.strike_step([100, 105, 110, 120, 125]) == 5
    with pytest.raises(ValueError):
        inst.strike_step([100])


def test_snapshot_chain_ivs_recover_the_smile(mock_ctx):
    today = market.ensure_instruments(mock_ctx)
    e = market.pick_expiry(inst.expiries(mock_ctx.engine, today, "STK01"), today, 10)
    snap = market.snapshot_chain(mock_ctx, "STK01", e)
    m = mock_ctx.broker.market
    assert snap.atm_iv == pytest.approx(m.atm_vol("STK01", SIM_NOW), rel=0.03)
    assert snap.fut_price > 0 and snap.T == pytest.approx((e - today).days / 365, abs=0.01)


def test_chain_demo_report(mock_ctx):
    text = option_chain_report(mock_ctx, "STK03")
    assert "SYNTHETIC MARKET" in text and "strike" in text and "ATM IV" in text
    assert "no stock options" in option_chain_report(mock_ctx, "NOPE")


def test_record_and_replay_fixtures(mock_ctx, tmp_path):
    out = record_fixtures(mock_ctx, ["STK01"], tmp_path / "fx", days=30)
    fb = FixtureBroker(out)
    nfo = fb.instruments("NFO")
    assert nfo and all(r["name"] == "STK01" for r in nfo)
    assert isinstance(nfo[0]["expiry"], date)
    key = f"NFO:{nfo[0]['tradingsymbol']}"
    assert key in fb.quote_raw([key, "NFO:MISSING"])
    tok = mock_ctx.broker.market.info("NSE:STK01").token
    hist = fb.historical(tok, SIM_NOW - timedelta(days=40), SIM_NOW, "day")
    assert hist and isinstance(hist[0]["date"], datetime)
