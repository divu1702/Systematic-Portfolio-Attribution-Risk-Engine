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
    assert "5 (latest)" in out.getvalue()
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
