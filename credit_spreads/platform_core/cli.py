"""`python -m platform_core <command>`: the platform's command line. Paper trading only.

  run              scheduler + control centre (+ Telegram commands such as /token when configured)
  serve            the control centre alone
  doctor           checks the laptop setup without printing any secret
  set-pin          set the control-centre PIN (stored hashed in .env)
  login            the daily Kite login (one session for both systems)
  record-fixtures  save the real-day candles for the two required detector tests
  job NAME         run one scheduled job now (close, gap, exits, margins, official, monthly, login-link,
                   events-reminder)
  status           the five accounts' headline numbers
Add --mock to run on the synthetic market (separate databases).
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .config import DEFAULT_PATH, PlatformConfigError, load_platform_config

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _platform(args, **kw):
    from app.logging_setup import setup_logging

    from . import runtime

    cfg = load_platform_config(args.config)
    p = runtime.build(cfg, mock=True if getattr(args, "mock", False) else None, **kw)
    setup_logging(p.ctx.cfg.log_dir, level="INFO" if getattr(args, "verbose", False) else "WARNING")
    return p


def _serve_thread(p, env=None) -> threading.Thread:
    import uvicorn

    from .control.app import create_app

    cc = p.cfg.control_centre
    server = uvicorn.Server(uvicorn.Config(create_app(p, env=env), host=cc.host, port=cc.port, log_level="warning",
                                           proxy_headers=True, forwarded_allow_ips="127.0.0.1"))
    t = threading.Thread(target=server.run, name="control-centre", daemon=True)
    t.start()
    return t


def cmd_run(args) -> int:
    from app.logging_setup import setup_logging

    from .scheduler import build_scheduler

    p = _platform(args)
    setup_logging(p.ctx.cfg.log_dir, level="INFO")
    sched = build_scheduler(p, blocking=False)
    sched.start()
    if not args.no_web:
        _serve_thread(p)
    cc = p.cfg.control_centre
    use_bot = bool(p.ctx.env.get("TELEGRAM_BOT_TOKEN") and p.ctx.env.get("TELEGRAM_CHAT_ID")) and not p.ctx.is_mock
    print(f"Platform running (paper only){' on the synthetic market' if p.ctx.is_mock else ''}. "
          f"go_live_date: {p.cfg.go_live_date or 'not set (no account trades yet)'}. "
          + ("" if args.no_web else f"Control centre on http://{cc.host}:{cc.port} (reach it through Tailscale). ")
          + ("Telegram bot on. " if use_bot else "Telegram not configured: alerts go to the log. ") + "Ctrl+C to stop.")
    try:
        if use_bot:
            from app.reporting.telegram_bot import run_bot

            run_bot(p.ctx)
        else:
            threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        print("stopped")
    finally:
        sched.shutdown(wait=False)
    return 0


def cmd_serve(args) -> int:
    p = _platform(args)
    t = _serve_thread(p)
    cc = p.cfg.control_centre
    print(f"Control centre on http://{cc.host}:{cc.port}. Ctrl+C to stop.")
    try:
        t.join()
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_set_pin(args) -> int:
    from .control import auth

    env_path = Path(args.env) if args.env else ENV_PATH
    pin = args.pin or getpass.getpass("New control-centre PIN (4-12 digits): ")
    if not args.pin and getpass.getpass("Repeat the PIN: ") != pin:
        print("The two PINs differ; nothing changed.")
        return 1
    try:
        h = auth.hash_pin(pin)
    except ValueError as exc:
        print(exc)
        return 1
    values = {"CONTROL_PIN_HASH": h}
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if not _has_value(existing, "CONTROL_SECRET"):
        values["CONTROL_SECRET"] = secrets.token_hex(32)
    auth.write_env(env_path, values)
    print(f"PIN saved (hashed) in {env_path}. Restart the platform to use it.")
    return 0


def _has_value(text: str, key: str) -> bool:
    for line in text.splitlines():
        if line.strip().startswith(f"{key}="):
            return bool(line.split("=", 1)[1].strip())
    return False


def cmd_login(args) -> int:
    from app.cli import main as oi_main

    cfg = load_platform_config(args.config)
    argv = ["--config", str(cfg.resolve(cfg.oi_system.config)), "login"]
    if args.token:
        argv += ["--token", args.token]
    return oi_main(argv)


def cmd_record_fixtures(args) -> int:
    from level_system import fixtures

    p = _platform(args)
    if not p.ctx.broker.session_valid():
        print("No Kite session today: run `python -m platform_core login` first.")
        return 1
    out = p.cfg.resolve(p.cfg.paths.fixtures)
    saved = fixtures.record(p.ds, out)
    for sym, n in saved.items():
        print(f"{sym}: {n} daily candles saved to {out / (sym + '.json')}")
    print("Now run: python -m pytest tests/test_level_realdays.py")
    return 0


def cmd_job(args) -> int:
    from . import scheduler as S

    p = _platform(args)
    fn = {"close": S.close_run, "gap": S.gap_check, "exits": S.exit_check, "margins": S.margins, "official": S.official,
          "login-link": S.login_link, "events-reminder": S.events_reminder,
          "monthly": lambda pl: __import__("level_system.comparison", fromlist=["write"]).write(pl, pl.today())}[args.name]
    res = fn(p)
    if hasattr(res, "as_dict"):
        d = res.as_dict()
        print(json.dumps({k: d[k] for k in ("phase", "live", "exits", "entries", "skipped", "armed", "removed", "warnings",
                                            "mismatches") if k in d}, indent=1, default=str)[:20000])
    else:
        print(res)
    return 0


def cmd_status(args) -> int:
    from app.fmt import inr

    from . import accounts

    p = _platform(args)
    try:
        rows = accounts.overview(p.broker, p.today())
    except Exception:                                  # no live quotes: saved marks only
        rows = [accounts.summary(p.broker, a, p.today(), marks={}) for a in p.cfg.accounts]
    print(f"go_live_date: {p.cfg.go_live_date or 'not set'}")
    from level_system.comparison import filter_review

    fr = filter_review(p)
    print(f"strict book: {fr['strict_closed']} closed trades (rule changes only after {fr['need_strict']}, "
          "apart from critical fixes)")
    for a in rows:
        if not a["started"]:
            print(f"{a['account']:15s} not started")
            continue
        wr = "–" if a["win_rate"] is None else f"{a['win_rate']:.0%}"
        print(f"{a['account']:15s} value {inr(a['value']):>12s}  today {inr(a['today_pnl']):>10s}  total {inr(a['total_pnl']):>10s}"
              f"  {a['pnl_r']:+.2f}R  margin {inr(a['used_margin'])}/{inr(a['available'])}  open {a['open_positions']}  win {wr}")
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run

    return run(args.config, sys.stdout, env_path=Path(args.env) if args.env else ENV_PATH)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m platform_core", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_PATH), help="platform.yaml")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run", help="scheduler + control centre")
    sp.add_argument("--mock", action="store_true")
    sp.add_argument("--no-web", action="store_true", help="scheduler only")
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("serve", help="control centre only")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=cmd_serve)
    sp = sub.add_parser("doctor", help="check the setup")
    sp.add_argument("--env", help=".env path")
    sp.set_defaults(func=cmd_doctor)
    sp = sub.add_parser("set-pin", help="set the control-centre PIN")
    sp.add_argument("--env", help=".env path")
    sp.add_argument("--pin", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_set_pin)
    sp = sub.add_parser("login", help="daily Kite login")
    sp.add_argument("--token", help="request_token or the redirect URL")
    sp.set_defaults(func=cmd_login)
    sp = sub.add_parser("record-fixtures", help="save the two real days for the detector tests")
    sp.set_defaults(func=cmd_record_fixtures)
    sp = sub.add_parser("job", help="run one scheduled job now")
    sp.add_argument("name", choices=["close", "gap", "exits", "margins", "official", "monthly", "login-link",
                                     "events-reminder"])
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=cmd_job)
    sp = sub.add_parser("status", help="the five accounts")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=cmd_status)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PlatformConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
