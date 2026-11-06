"""Command-line entry point: `python -m app <command>`. Add `--mock` to any market command to
run it against the synthetic market (separate database, no Kite session needed)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, load_config
from .logging_setup import setup_logging

log = logging.getLogger("app")


def _ctx(args, **kw):
    from .context import build_context

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir, level="WARNING" if not getattr(args, "verbose", False) else "INFO")
    return build_context(cfg, mock=True if getattr(args, "mock", False) else None, **kw)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(args.config, sys.stdout)


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .data import migrations

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir)
    applied = migrations.upgrade(cfg.db_path)
    if applied:
        for m in applied:
            print(f"applied {m.version:04d}_{m.name}")
    else:
        print("database already up to date")
    print(f"database: {cfg.db_path}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from .broker.kite import KiteBroker
    from .broker.login import wait_for_redirect
    from .clock import SystemClock
    from .env import load_env

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir)
    broker = KiteBroker(cfg, load_env(), SystemClock())
    if args.token:
        broker.complete_login(args.token)
        print("Kite session saved for today.")
        return 0
    if broker.session_valid() and not args.force:
        print("A Kite session for today already exists (use --force to log in again).")
        return 0
    print("Open this URL, log in and complete 2FA:\n")
    print(f"  {broker.login_url()}\n")
    print(f"Waiting up to {args.timeout}s for the redirect to {cfg.broker.redirect_url} ...")
    print("From a phone the redirect page will not load; copy the request_token from the address")
    print("bar and run:  python -m app login --token <request_token>")
    url = wait_for_redirect(cfg.broker.redirect_url, timeout=args.timeout)
    if not url:
        print("Timed out waiting for the redirect.")
        return 1
    broker.complete_login(url)
    print("Kite session saved for today.")
    return 0


def _cmd_chain(args: argparse.Namespace) -> int:
    from .views import option_chain_report

    ctx = _ctx(args)
    print(option_chain_report(ctx, args.symbol.upper(), date.fromisoformat(args.expiry) if args.expiry else None,
                              rows=args.rows))
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from .jobs import morning_refresh, scan_job
    from .views import scan_report

    if args.mock and args.warmup:
        from datetime import datetime, time as dtime
        from .clock import SystemClock, parse_hhmm
        from .sim import make_sim_context, sim_days, warmup

        cfg = load_config(args.config)
        setup_logging(cfg.log_dir, level="WARNING")
        from .broker.synthetic import SyntheticMarket

        today = SystemClock().today()
        market_ = SyntheticMarket(seed=cfg.broker.mock_seed, n_symbols=cfg.broker.mock_symbols, anchor=today,
                                  r=cfg.analytics.risk_free)
        last = max(d for d in market_.days if d <= today)
        days = sim_days(market_, last, args.warmup + 1)
        ctx = make_sim_context(cfg, days[0], market=market_)
        print(f"Warming up the synthetic market for {args.warmup} trading days "
              f"({days[0]} to {days[-2]}) so IV history exists ...", flush=True)
        warmup(ctx, days[:-1], progress=lambda i, n: print(f"  day {i}/{n}", end="\r", flush=True))
        print()
        ctx.clock.set(datetime.combine(days[-1], dtime(8, 45)))
        morning_refresh(ctx)
        ctx.clock.set(datetime.combine(days[-1], parse_hhmm(cfg.schedule.scan)))
    else:
        ctx = _ctx(args)
        morning_refresh(ctx)
    result = scan_job(ctx)
    print(scan_report(ctx, result, show_rejections=args.why))
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from .recorder import record_fixtures

    ctx = _ctx(args)
    out = record_fixtures(ctx, [s.upper() for s in args.symbols], ctx.cfg.resolve(ctx.cfg.paths.fixtures))
    print(f"fixtures written to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description="NSE stock credit spread system")
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="log INFO to the console")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="print config and database status").set_defaults(func=_cmd_doctor)
    sub.add_parser("migrate", help="create or upgrade the SQLite database").set_defaults(func=_cmd_migrate)

    sp = sub.add_parser("login", help="daily Kite login (2FA)")
    sp.add_argument("--token", help="request_token, or the full redirect URL from the browser")
    sp.add_argument("--timeout", type=int, default=300)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=_cmd_login)

    sp = sub.add_parser("chain", help="print today's option chain for one stock")
    sp.add_argument("symbol")
    sp.add_argument("--expiry", help="YYYY-MM-DD (default: nearest monthly with enough days left)")
    sp.add_argument("--rows", type=int, default=10, help="strikes either side of the money")
    sp.add_argument("--mock", action="store_true", help="use the synthetic market")
    sp.set_defaults(func=_cmd_chain)

    sp = sub.add_parser("scan", help="run the entry scan: ranked candidates and rejection funnel")
    sp.add_argument("--mock", action="store_true", help="use the synthetic market")
    sp.add_argument("--warmup", type=int, default=0,
                    help="with --mock: simulate N prior trading days first (fresh mock database)")
    sp.add_argument("--why", type=int, nargs="?", const=1, default=0, help="list each stock's rejection reason")
    sp.set_defaults(func=_cmd_scan)

    sp = sub.add_parser("record", help="save real Kite responses as offline test fixtures")
    sp.add_argument("symbols", nargs="+")
    sp.set_defaults(func=_cmd_record, mock=False)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
