import io
import logging

import pytest

from app.cli import main
from app.doctor import run_doctor
from app.env import load_env, parse_env_file
from app.fmt import inr
from app.logging_setup import setup_logging


@pytest.mark.parametrize("amount, expected", [
    (0, "₹0"), (999, "₹999"), (14850, "₹14,850"), (39150, "₹39,150"),
    (500000, "₹5,00,000"), (12345678, "₹1,23,45,678"), (-39150, "-₹39,150"),
])
def test_inr_grouping(amount, expected):
    assert inr(amount) == expected


def test_env_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nKITE_API_KEY=abc\nexport KITE_API_SECRET="s e c"\nX=1 # trailing\n\n')
    assert parse_env_file(p) == {"KITE_API_KEY": "abc", "KITE_API_SECRET": "s e c", "X": "1"}
    merged = load_env(p, environ={"KITE_API_KEY": "from-env"})
    assert merged["KITE_API_KEY"] == "from-env"


def test_env_rejects_malformed_line(tmp_path):
    p = tmp_path / ".env"
    p.write_text("NOEQUALS\n")
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_env_file(p)


def test_doctor_before_and_after_migrate(raw_config, write_config, tmp_path):
    cfg_path = write_config(raw_config)
    secret = "do-not-print-me"
    (tmp_path / ".env").write_text(f"KITE_API_KEY={secret}\n")

    out = io.StringIO()
    assert run_doctor(cfg_path, out, env_path=tmp_path / ".env") == 0
    text = out.getvalue()
    assert "PAPER" in text and "₹5,00,000" in text
    assert "not created" in text and "0.0350%" in text
    assert secret not in text

    assert main(["--config", str(cfg_path), "migrate"]) == 0
    out = io.StringIO()
    assert run_doctor(cfg_path, out, env_path=tmp_path / ".env") == 0
    assert "6 (latest)" in out.getvalue()
    assert (tmp_path / "data" / "test.db").exists()


def test_doctor_reports_invalid_config(raw_config, write_config):
    raw_config["risk"]["per_trade"] = 5
    out = io.StringIO()
    assert run_doctor(write_config(raw_config), out) == 2
    assert "INVALID" in out.getvalue() and "risk.per_trade" in out.getvalue()


def test_logging_writes_file(tmp_path):
    setup_logging(tmp_path, console=False)
    logging.getLogger("t").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello" in (tmp_path / "app.log").read_text()
    setup_logging(None, console=False)


def test_usd_formatting_and_reset():
    from app.fmt import money, set_currency
    set_currency("USD")
    try:
        assert money(1234567) == "$1,234,567" and money(-950) == "-$950"
    finally:
        set_currency("INR")
    assert money(500000) == "₹5,00,000"


def test_cli_overrides_validate(cfg):
    from app.cli import apply_overrides
    from app.config import ConfigError

    c = apply_overrides(cfg, ["iv.min_iv_hv_ratio=1.15", "risk.gap_cap_per_trade=null"])
    assert c.iv.min_iv_hv_ratio == 1.15 and c.risk.gap_cap_per_trade is None
    with pytest.raises(ConfigError, match="unknown config key"):
        apply_overrides(cfg, ["iv.nope=1"])
    with pytest.raises(ConfigError, match="invalid override"):
        apply_overrides(cfg, ["strikes.delta_min=0.9"])


def test_config_hash_tracks_thresholds(cfg):
    from app.cli import apply_overrides

    assert cfg.strategy_hash() == cfg.strategy_hash()
    assert apply_overrides(cfg, ["iv.min_ivp=60"]).strategy_hash() != cfg.strategy_hash()
    assert apply_overrides(cfg, ["ops.backup_keep=3"]).strategy_hash() == cfg.strategy_hash()


def test_nse_client_identifies_honestly():
    from types import SimpleNamespace

    from app.data.nse import HEADERS, NSEClient, NSEError

    assert "Mozilla" not in HEADERS["User-Agent"] and "Referer" not in HEADERS

    class S:
        headers = {}
        calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append(url)
            return SimpleNamespace(status_code=403, json=lambda: {})

    s = S()
    client = NSEClient(session=s)
    with pytest.raises(NSEError, match="imports/events"):
        client.holidays()
    assert len(s.calls) == 1                         # no home-page warm-up, no retry around the block


def test_event_csv_imports_and_freshness(mock_ctx, tmp_path):
    import os
    from datetime import datetime, timedelta

    from app.data import events
    from app.data.imports import parse_event_csv

    folder = mock_ctx.cfg.resolve(mock_ctx.cfg.paths.imports) / "events"
    folder.mkdir(parents=True)
    f = folder / "board_meetings.csv"
    f.write_text('"SYMBOL","COMPANY NAME","PURPOSE","BOARD MEETING DATE","DATE OF SUBMISSION"\n'
                 '"STK01","Stock One Ltd","Financial Results","20-Nov-2026","10-Nov-2026"\n')
    rows = parse_event_csv(f)
    assert rows[0]["symbol"] == "STK01" and rows[0]["event_type"] == "results"
    assert rows[0]["event_date"].isoformat() == "2026-11-20"
    stamp = mock_ctx.clock.now() - timedelta(hours=3)
    os.utime(f, (stamp.timestamp(), stamp.timestamp()))
    mock_ctx.nse.fail = True                         # NSE refuses: the export keeps F2 fresh
    events.fetch_events(mock_ctx)
    assert events.is_fresh(mock_ctx.engine, mock_ctx.clock.now(), 2)
    chk = events.check_f2(mock_ctx, "STK01", __import__("datetime").date(2026, 12, 29))
    assert not chk.passed and "2026-11-20" in chk.reason


def test_backup_and_heartbeat(mock_ctx):
    from app.ops import backup_database, heartbeat
    from app.risk.equity import eod_equity

    assert backup_database(mock_ctx) is None and heartbeat(mock_ctx, eod_equity(mock_ctx)) is None   # mock: off
    mock_ctx.is_mock = False
    try:
        path = backup_database(mock_ctx, keep=2)
        assert path and path.exists()
        msg = heartbeat(mock_ctx, eod_equity(mock_ctx))
        assert msg.startswith("EOD") and mock_ctx.notifier.sent[-1][1] == msg
    finally:
        mock_ctx.is_mock = True
