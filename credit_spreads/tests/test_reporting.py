from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.data import state
from app.execution.entries import enter_from_scan
from app.execution.paper import PaperExecutor
from app.reporting import approvals, charts, metrics
from app.reporting.dashboard import load as dashboard_load
from app.reporting.telegram_bot import TelegramNotifier, handle_command
from app.reporting.weekly import build_report, write_report
from app.risk.equity import eod_equity
from app.risk.limits import open_trades
from app.sim import run_trading
from app.strategy.scanner import run_scan
from tests.conftest import SIM_NOW, relaxed_ctx


def T(pnl, max_loss=10_000, be=0.7, p=0.75, side="bull_put", ivhv=1.3, method="proxy", reason="profit_target",
      days=0, mae=-500, credit=3.0):
    return SimpleNamespace(pnl_net=pnl, max_loss=max_loss, breakeven_wr=be, p_model=p, side=side, iv_hv=ivhv,
                           ivp_method=method, exit_reason=reason, replayed=False, symbol="X", trade_id=f"t{pnl}{days}",
                           entry_ts=datetime(2026, 1, 1) + timedelta(days=days),
                           exit_ts=datetime(2026, 1, 5) + timedelta(days=days), mae=mae, entry_credit=credit,
                           lots=1, lot_size=1000)


# ----------------------------------------------------------------------------- metrics

def test_go_live_bars(mock_ctx):
    wins = [T(1600, days=i * 4) for i in range(24)]
    losses = [T(-2500, reason="hard_stop", days=i * 4 + 1) for i in range(8)]
    ms = {m.name: m for m in metrics.go_live(mock_ctx.engine, 500_000, wins + losses)}
    assert ms["Closed paper trades"].passed                       # 32
    assert ms["Calendar span"].passed                             # ~3.1 months
    exp = (24 * 1600 - 8 * 2500) / 32 / 10_000
    assert ms["Expectancy"].value == pytest.approx(exp) and ms["Expectancy"].passed
    assert ms["Win rate vs breakeven"].value == pytest.approx(0.75 - 0.70)
    assert not ms["Win rate vs breakeven"].passed                 # needs > 5 pp
    assert ms["Profit factor"].value == pytest.approx(38_400 / 20_000) and ms["Profit factor"].passed
    assert ms["Worst single trade"].value == pytest.approx(2500 / 500_000)
    assert ms["Fill realism"].passed is None                      # reported, not a gate


def test_go_live_with_no_trades(mock_ctx):
    ms = metrics.go_live(mock_ctx.engine, 500_000, [])
    assert not any(m.passed for m in ms if m.passed is not None and m.name != "Maximum drawdown")


def test_calibration_and_monotonic():
    ts = [T(100 if i % 10 < (i // 10) + 3 else -100, p=0.5 + i / 200) for i in range(60)]
    rows = metrics.calibration(ts, bins=6)
    assert len(rows) == 6 and sum(r["n"] for r in rows) == 60
    assert [r["p_model"] for r in rows] == sorted(r["p_model"] for r in rows)
    assert metrics.is_monotonic(rows) is True
    assert metrics.is_monotonic([{"actual": 0.5}, {"actual": 0.4}]) is False
    assert metrics.is_monotonic([]) is None


def test_splits_and_buckets():
    sp = metrics.splits([T(100, side="bear_call", ivhv=1.6), T(-50, ivhv=1.1, reason="hard_stop")])
    assert {r["key"] for r in sp["Side"]} == {"bear call", "bull put"}
    assert {r["key"] for r in sp["IV/HV at entry"]} == {"1.50-2.00", "< 1.25"}
    assert metrics.iv_hv_bucket(2.5) == ">= 2.00" and metrics.iv_hv_bucket(None) == "unknown"


def test_mae_multiples():
    assert metrics.mae_multiples([T(0, mae=-3000, credit=3.0), T(0, mae=200)]) == [1.0, 0.0]


# ----------------------------------------------------------------------------- charts

def test_charts_render_marks_with_tooltips():
    svg = charts.line_chart([(date(2026, 1, i), 100 + i) for i in range(1, 8)])
    assert svg.startswith("<svg") and svg.count("<title>") == 7 and 'stroke-width="2"' in svg
    assert "No data" in charts.line_chart([])
    bars = charts.bar_rows([("a", 5), ("b", -3)], diverging=True)
    assert 'class="bar neg"' in bars and 'class="bar pos"' in bars and "title=" in bars
    hist = charts.histogram([0.1, 0.2, 1.1, 3.0], [0, 0.5, 1, 2])
    assert hist.count("<title>") == 2          # [0,0.5): 2 values, >= 1: 2 values
    assert charts.compact(450_000) == "4.5L" and charts.compact(12_000) == "12K"


# ----------------------------------------------------------------------------- weekly report on a real run

@pytest.fixture
def ran(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    m = ctx.broker.market
    i = m.day_index[SIM_NOW.date()] + 1
    run_trading(ctx, m.days[i:i + 5])
    return ctx


def test_weekly_report_renders(ran, tmp_path):
    html = build_report(ran, ran.clock.today())
    for needle in ("Go-live scorecard", "Equity", "Drawdown", "Rejection funnel", "MAE distribution", "Calibration",
                   "Synthetic market", "Net P&amp;L by group", "--series-1", "prefers-color-scheme: dark"):
        assert needle in html
    path = write_report(ran, out_dir=tmp_path / "r")
    assert path.exists() and path.name.endswith("_mock.html")


def test_funnel_counts_stock_scans(ran):
    f = metrics.funnel(ran.engine)
    assert list(f) == metrics.STAGES
    assert sum(f.values()) > 0


def test_dashboard_loader_reads_db(ran):
    d = dashboard_load(str(ran.engine.url.database))
    assert set(d) == {"open", "closed", "equity", "candidates", "state"}
    assert len(d["equity"]) == 5


def test_dashboard_renders_with_streamlit(ran):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    import app.reporting.dashboard as dash

    at = AppTest.from_file(dash.__file__, default_timeout=60)
    import sys
    old = sys.argv
    sys.argv = ["dashboard", "--db", str(ran.engine.url.database)]
    try:
        at.run()
    finally:
        sys.argv = old
    assert not at.exception
    assert any("Credit spread system" in t.value for t in at.title)


# ----------------------------------------------------------------------------- Telegram

class FakeSession:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return SimpleNamespace(status_code=self.status)


def test_telegram_notifier_posts_and_survives_errors():
    s = FakeSession()
    n = TelegramNotifier("TOKEN", "42", session=s)
    n.send("ENTRY ABC bull put", "entry")
    url, body = s.posts[0]
    assert "botTOKEN/sendMessage" in url and body["chat_id"] == "42" and body["text"].startswith("ENTRY")
    n.send("drawdown", "risk")
    assert s.posts[1][1]["text"] == "RISK: drawdown"
    bad = TelegramNotifier("T", "1", session=FakeSession(status=500))
    bad.send("x", "exit")                       # no exception
    assert bad.failures == 1 and bad.sent[-1] == ("exit", "x")


def test_bot_ignores_strangers_and_kills(mock_ctx):
    mock_ctx.env = {"TELEGRAM_CHAT_ID": "42"}
    assert handle_command(mock_ctx, "/kill", "999") is None
    assert not state.flag(mock_ctx.engine, state.KILLED)
    assert "Kill switch ON" in handle_command(mock_ctx, "/kill market looks wild", "42")
    assert state.flag(mock_ctx.engine, state.KILLED) == "market looks wild"
    assert "BLOCKED" in handle_command(mock_ctx, "/status", "42")
    handle_command(mock_ctx, "/resume", "42")
    assert not state.flag(mock_ctx.engine, state.KILLED)
    state.put(mock_ctx.engine, state.DD_PAUSED, "dd")
    handle_command(mock_ctx, "/resume_drawdown", "42")
    assert not state.flag(mock_ctx.engine, state.DD_PAUSED)
    assert "kite.zerodha.com" in handle_command(mock_ctx, "/login", "42")
    assert "saved" in handle_command(mock_ctx, "/token abc123", "42")
    assert "Usage" in handle_command(mock_ctx, "/token", "42")
    assert "/kill" in handle_command(mock_ctx, "/help", "42")
    assert "Unknown" in handle_command(mock_ctx, "/foo", "42")
    assert "Report written" in handle_command(mock_ctx, "/report", "42", report_fn=lambda c: "r.html")


def test_failed_login_alerts(mock_ctx):
    mock_ctx.env = {"TELEGRAM_CHAT_ID": "42"}

    def boom(_):
        raise RuntimeError("invalid token")
    mock_ctx.broker.complete_login = boom
    assert "failed" in handle_command(mock_ctx, "/token bad", "42")
    assert mock_ctx.notifier.sent[-1][0] == "login"


def test_proposals_approve_reject_expire(mock_ctx):
    mock_ctx.env = {"TELEGRAM_CHAT_ID": "42"}
    a = approvals.create_proposal(mock_ctx, "STK01", "bull_put", "STK01 bull put 500/490", {"x": 1})
    b = approvals.create_proposal(mock_ctx, "STK02", "bear_call", "STK02 bear call 700/710", {})
    assert mock_ctx.notifier.sent[-1][0] == "proposal"
    assert f"#{a}" in handle_command(mock_ctx, "/pending", "42")
    assert "approved" in handle_command(mock_ctx, f"/approve {a}", "42")
    assert "already approved" in handle_command(mock_ctx, f"/reject {a}", "42")
    mock_ctx.clock.set(SIM_NOW + timedelta(minutes=mock_ctx.cfg.live.approval_timeout_min + 1))
    ok, msg = approvals.decide(mock_ctx, b, True)
    assert not ok and "expired" in msg
    assert "No proposal" in handle_command(mock_ctx, "/approve 999", "42")
    assert "Usage" in handle_command(mock_ctx, "/approve x", "42")
