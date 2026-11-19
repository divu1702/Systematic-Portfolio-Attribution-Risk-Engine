"""The phone control centre: PIN login and lockout, the five-account overview, positions and funds,
the discretionary order ticket (preview, place, close), the journal, the ROI scanner, reports and the
events-file upload. Paper only: every order goes through the shared paper broker."""

import io
import json
from urllib.parse import unquote
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SimClock
from app.context import build_context
from app.data.calendar import TradingCalendar
from app.jobs import catch_up_daily, morning_refresh
from platform_core import runtime
from platform_core.control import auth
from platform_core.control.app import create_app, sparkline
from platform_core.models import PTrade, Upload
from tests.conftest import shared_market

PIN = "482916"
RUN = datetime(2026, 11, 6, 11, 0)


@pytest.fixture
def plat(cfg, platform_cfg, tmp_path):
    ctx = build_context(cfg, mock=True, clock=SimClock(RUN), db_path=tmp_path / "m.db", env={}, market=shared_market())
    morning_refresh(ctx)
    catch_up_daily(ctx, TradingCalendar.load(ctx.engine).prev_trading_day(ctx.clock.today()))
    return runtime.build(platform_cfg.model_copy(update={"go_live_date": date(2026, 11, 2)}), ctx=ctx)


def loc(r) -> str:
    return unquote(r.headers["location"])


def client(p, pin_hash=None, secret="s3cret-for-tests"):
    env = {"CONTROL_PIN_HASH": pin_hash if pin_hash is not None else auth.hash_pin(PIN, iterations=1000),
           "CONTROL_SECRET": secret}
    return TestClient(create_app(p, env=env))


def logged_in(p):
    c = client(p)
    r = c.post("/login", data={"pin": PIN}, follow_redirects=False)
    assert r.status_code == 303 and loc(r) == "/"
    return c


def test_pin_hash_cookie_and_env_file(tmp_path):
    h = auth.hash_pin("1234", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "1234" not in h
    assert auth.verify_pin("1234", h) and not auth.verify_pin("1235", h) and not auth.verify_pin("1234", "junk")
    with pytest.raises(ValueError):
        auth.hash_pin("12a4")
    tok = auth.sign("k", now=1000.0)
    assert auth.check("k", tok, now=1001.0) and not auth.check("other", tok, now=1001.0)
    assert not auth.check("k", tok, now=1000.0 + auth.SESSION_SECONDS + 1) and not auth.check("k", "a.b.c")
    env = tmp_path / ".env"
    env.write_text("KITE_API_KEY=abc\n# CONTROL_SECRET=old\nCONTROL_PIN_HASH=\n")
    auth.write_env(env, {"CONTROL_PIN_HASH": h, "CONTROL_SECRET": "xyz"})
    lines = env.read_text().splitlines()
    assert "KITE_API_KEY=abc" in lines and f"CONTROL_PIN_HASH={h}" in lines and "CONTROL_SECRET=xyz" in lines
    assert "# CONTROL_SECRET=old" in lines


def test_everything_needs_the_pin(plat):
    c = client(plat)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/api/overview").status_code == 401
    assert c.get("/positions", follow_redirects=False).status_code == 303
    r = c.post("/login", data={"pin": "000000"}, follow_redirects=False)
    assert "Wrong" in loc(r) or "msg=" in loc(r)
    for _ in range(4):
        c.post("/login", data={"pin": "000000"})
    assert auth.Lockout(plat.pengine).locked() > 0
    assert c.post("/login", data={"pin": PIN}, follow_redirects=False).headers["location"] == "/login"   # locked out
    nopin = client(plat, pin_hash="")
    assert "set-pin" in nopin.get("/login").text and nopin.get("/api/overview").status_code == 401


def test_overview_and_positions(plat):
    c = logged_in(plat)
    html = c.get("/").text
    for acc in plat.cfg.accounts:
        assert acc in html
    assert "location.reload" in html                                       # live values refresh every 30 s
    data = c.get("/api/overview").json()
    assert [a["account"] for a in data] == list(plat.cfg.accounts) and all(a["value"] == 500_000 for a in data)
    page = c.get("/positions?account=level_strict").text
    assert "Funds ledger" in page and "opening" in page
    assert c.get("/positions?account=nope").status_code == 404


def test_order_ticket_place_close_and_journal(plat):
    c = logged_in(plat)
    ex = c.get("/api/expiries?symbol=STK05").json()
    assert ex["chosen"] in ex["expiries"]
    ch = c.get(f"/api/chain?symbol=STK05&expiry={ex['chosen']}").json()
    puts = [r["strike"] for r in ch["rows"] if "PE" in r and r["strike"] < ch["spot"]]
    short, long_ = puts[-2], puts[-5]
    form = {"symbol": "stk05", "expiry": ex["chosen"], "direction": "bull_put", "short": short, "long": long_,
            "lots": "1", "stop": "", "tag": "2", "reason": "test"}
    pv = c.post("/api/preview", data=form).json()
    assert pv["credit_natural"] > 0 and pv["margin"] > 0 and pv["one_r_basis"] == "maximum loss"
    assert pv["one_r"] == pytest.approx(pv["max_loss"])
    bad = c.post("/api/preview", data={**form, "long": short + 10})
    assert bad.status_code == 400 and "lower strike" in bad.json()["detail"]
    with_stop = c.post("/api/preview", data={**form, "stop": str(round(ch["spot"] * 0.97, 1))}).json()
    assert with_stop["one_r_basis"] == "stop level" and with_stop["one_r"] < with_stop["max_loss"]
    r = c.post("/ticket/place", data=form, follow_redirects=False)
    assert "Filled" in loc(r)
    t = plat.broker.open_trades("discretionary")[0]
    assert t.source == "discretionary" and t.setup_tag == "2" and t.reason == "test" and t.one_r == pytest.approx(t.max_loss)
    ctx = json.loads(t.context_json)
    assert ctx["entry"]["spot"] and "short" in ctx["oi"]
    assert "Close position" in c.get("/positions?account=discretionary").text
    # Journal: a stop level re-prices 1R with the entry values.
    stop = round(ch["spot"] * 0.97, 1)
    assert "Saved" in c.post(f"/journal/{t.id}", data={"tag": "none", "reason": "edited", "stop": str(stop)},
                             follow_redirects=False).headers["location"]
    t2 = plat.broker.get_trade(t.id)
    assert t2.setup_tag == "none" and t2.reason == "edited" and t2.owner_stop == stop and t2.one_r < t2.max_loss
    assert "edited" in c.get("/journal").text
    r = c.post(f"/close/{t.id}", follow_redirects=False)
    assert "Closed" in loc(r)
    closed = plat.broker.get_trade(t.id)
    assert closed.status == "closed" and closed.exit_reason == "owner_close" and closed.pnl_r is not None
    assert "exit" in json.loads(closed.context_json)
    other = plat.broker.open_trades()
    assert not other


def test_only_discretionary_positions_close_from_the_app(plat):
    from tests.test_platform_broker import bear_call

    plat.broker.open_spread(bear_call(plat.ds, "level_strict"))
    t = plat.broker.open_trades("level_strict")[0]
    c = logged_in(plat)
    r = c.post(f"/close/{t.id}", follow_redirects=False)
    assert "only discretionary" in loc(r)
    assert plat.broker.get_trade(t.id).status == "open"


def test_ticket_rejected_before_go_live(cfg, platform_cfg, tmp_path):
    ctx = build_context(cfg, mock=True, clock=SimClock(RUN), db_path=tmp_path / "n.db", env={}, market=shared_market())
    morning_refresh(ctx)
    p = runtime.build(platform_cfg, ctx=ctx)
    c = logged_in(p)
    ex = c.get("/api/expiries?symbol=STK05").json()
    ch = c.get(f"/api/chain?symbol=STK05&expiry={ex['chosen']}").json()
    calls = [r["strike"] for r in ch["rows"] if "CE" in r and r["strike"] > ch["spot"]]
    r = c.post("/ticket/place", data={"symbol": "STK05", "expiry": ex["chosen"], "direction": "bear_call",
                                      "short": calls[1], "long": calls[3], "lots": "1"}, follow_redirects=False)
    assert "Rejected" in loc(r) and not p.broker.open_trades()


def test_scanner_reports_and_uploads(plat, tmp_path):
    plat.ctx.clock.set(RUN.replace(hour=15, minute=20))
    plat.engine.run_close()
    c = logged_in(plat)
    page = c.get("/scanner").text
    assert "Screener of 2026-11-06" in page and "STK05" in page
    assert "Daily screener reports" in c.get("/reports").text
    assert c.get("/reports/file/daily/..%2F..%2Fconfig.html").status_code == 404
    csv = "SYMBOL,COMPANY NAME,PURPOSE,BOARD MEETING DATE\nSTK01,Stock One,Financial Results,20-Nov-2026\n"
    r = c.post("/upload", data={"kind": "events"}, files={"file": ("board meetings.csv", io.BytesIO(csv.encode()), "text/csv")},
               follow_redirects=False)
    assert "Saved" in loc(r)
    assert (plat.ds.events_import_dir() / "board_meetings.csv").exists()
    assert plat.ds.newest_events_file_age_days() < 0.01
    bad = c.post("/upload", data={"kind": "events"}, files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
                 follow_redirects=False)
    assert "Not saved" in loc(bad)
    sec = c.post("/upload", data={"kind": "sectors"},
                 files={"file": ("sectors.csv", io.BytesIO(b"symbol,sector\nSTK01,Banks\n"), "text/csv")}, follow_redirects=False)
    assert "1 rows" in loc(sec)
    with Session(plat.pengine) as s:
        assert {u.kind for u in s.scalars(select(Upload))} == {"events", "sectors"}
    assert "Recent uploads" in c.get("/upload").text


def test_sparkline():
    assert "Equity curve after" in sparkline([{"date": "2026-11-06", "value": 1.0}])
    svg = sparkline([{"date": "a", "value": 1.0}, {"date": "b", "value": 2.0}, {"date": "c", "value": 1.5}])
    assert svg.startswith("<svg") and "polyline" in svg
