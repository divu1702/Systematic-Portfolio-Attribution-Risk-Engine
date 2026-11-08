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


def _cmd_run(args: argparse.Namespace) -> int:
    from .scheduler import build_scheduler

    ctx = _ctx(args)
    setup_logging(ctx.cfg.log_dir, level="INFO")
    use_bot = bool(ctx.env.get("TELEGRAM_BOT_TOKEN") and ctx.env.get("TELEGRAM_CHAT_ID")) and not ctx.is_mock
    sched = build_scheduler(ctx, blocking=not use_bot)
    print(f"Scheduler running in {ctx.cfg.mode.upper()} mode{' on the synthetic market' if ctx.is_mock else ''}"
          f"{' with the Telegram bot' if use_bot else ' (Telegram not configured: alerts go to the log)'}. "
          "Ctrl+C to stop.")
    try:
        if use_bot:
            from .reporting.telegram_bot import run_bot

            sched.start()
            run_bot(ctx)
        else:
            sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("stopped")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .reporting.weekly import write_report

    if args.db:
        from .context import build_context

        cfg = load_config(args.config)
        ctx = build_context(cfg, mock=True, db_path=Path(args.db), env={})
    else:
        ctx = _ctx(args)
    asof = date.fromisoformat(args.asof) if args.asof else None
    if asof is None and args.db:
        from .reporting.metrics import equity_series
        eq = equity_series(ctx.engine)
        asof = eq[-1].date if eq else None
    print(f"report written to {write_report(ctx, asof)}")
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    from .jobs import monitor_job

    ctx = _ctx(args)
    msgs = monitor_job(ctx, args.phase)
    print("\n".join(msgs) if msgs else "No exits.")
    return 0


def _cmd_eod(args: argparse.Namespace) -> int:
    from .jobs import eod_job

    ctx = _ctx(args)
    p = eod_job(ctx)
    print(f"equity {p.equity:,.0f}  realised {p.realised:,.0f}  unrealised {p.unrealised:,.0f}  "
          f"drawdown {p.drawdown:.1%}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from .views import status_report

    print(status_report(_ctx(args)))
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    from .data import state

    ctx = _ctx(args)
    state.put(ctx.engine, state.KILLED, args.reason or "manual kill from the command line")
    print("Kill switch set: no new entries. Exits keep running. Undo with `python -m app resume`.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    from .data import state

    ctx = _ctx(args)
    state.clear(ctx.engine, state.KILLED)
    msg = "Kill switch cleared."
    if args.drawdown:
        state.clear(ctx.engine, state.DD_PAUSED)
        msg += " Drawdown pause reset."
    print(msg)
    return 0


def _cmd_sim(args: argparse.Namespace) -> int:
    from .broker.synthetic import SyntheticMarket
    from .clock import SystemClock
    from .sim import make_sim_context, run_trading, sim_days, warmup
    from .views import sim_summary

    cfg = load_config(args.config)
    if args.capital:
        cfg = cfg.model_copy(update={"capital": float(args.capital)})
    if args.symbols:
        cfg = cfg.model_copy(update={"broker": cfg.broker.model_copy(update={"mock_symbols": args.symbols})})
    setup_logging(cfg.log_dir, level="WARNING")
    today = SystemClock().today()
    m = SyntheticMarket(seed=args.seed if args.seed is not None else cfg.broker.mock_seed,
                        n_symbols=cfg.broker.mock_symbols, anchor=today, r=cfg.analytics.risk_free)
    last = max(d for d in m.days if d <= today)
    days = sim_days(m, last, args.warmup + args.days)
    ctx = make_sim_context(cfg, days[0], market=m, db_path=Path(args.db) if args.db else None)
    print(f"Synthetic market, seed {m.seed}: warm-up {args.warmup} days, then {args.days} trading days "
          f"({days[args.warmup]} to {days[-1]}), capital {cfg.capital:,.0f}.", flush=True)
    warmup(ctx, days[:args.warmup], progress=lambda i, n: print(f"  warm-up {i}/{n}", end="\r", flush=True))
    print()
    results = run_trading(ctx, days[args.warmup:], outage_prob=args.outages,
                          progress=lambda i, n, r: print(f"  trading {i}/{n}", end="\r", flush=True))
    print()
    print(sim_summary(ctx, results))
    return 0


def apply_overrides(cfg, pairs):
    """`section.key=value` overrides (YAML-parsed values), re-validated as a whole config."""
    import yaml

    from .config import Config

    if not pairs:
        return cfg
    raw = cfg.model_dump()
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep:
            raise ConfigError(f"override {pair!r} must look like section.key=value")
        node = raw
        parts = key.strip().split(".")
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                raise ConfigError(f"unknown config section in override {key!r}")
            node = node[part]
        if parts[-1] not in node:
            raise ConfigError(f"unknown config key in override {key!r}")
        node[parts[-1]] = yaml.safe_load(val)
    base_dir = raw.pop("base_dir", cfg.base_dir)
    try:
        return Config(**raw, base_dir=base_dir)
    except Exception as exc:
        raise ConfigError(f"invalid override: {exc}") from exc


def _cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import runner
    from .backtest.broker import read_universe
    from .backtest.dolthub import DoltHubClient, load_chains, load_reference
    from .views import backtest_summary

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir, level="WARNING" if not args.verbose else "INFO")
    nse = args.market == "nse"
    store = cfg.resolve(args.store or (cfg.paths.market_store if nse else "data/market_us.db"))
    if nse and not store.exists():
        print(f"no recorded NSE data at {store} yet: it fills up from each 15:00 scan while `run` is going")
        return 1
    if args.universe:
        universe = read_universe(Path(args.universe))
    else:
        universe = runner.store_universe(store) if nse else read_universe(runner.UNIVERSE_US)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.action == "load":
        if nse:
            print("NSE data is recorded by the live system, not downloaded; `load` is for the US data only")
            return 1
        from datetime import timedelta

        client = DoltHubClient()
        print(f"Loading {len(universe)} symbols from DoltHub into {store} ...", flush=True)
        load_reference(client, store, list(universe), start - timedelta(days=600), end)
        n = load_chains(client, store, list(universe), start, end,
                        progress=lambda i, n: print(f"  chain batches {i}/{n}", end="\r", flush=True))
        print(f"\nloaded {n:,} option quotes")
        return 0
    base = runner.nse_config(cfg, args.capital) if nse else runner.us_config(cfg, capital=args.capital or 100_000)
    bt_cfg = apply_overrides(base, args.set)
    days = runner.trading_days(store, start, end, symbols=list(universe))
    if len(days) <= args.warmup:
        print(f"only {len(days)} trading days with data in range; need more than the {args.warmup}-day warm-up")
        return 1
    db = Path(args.db) if args.db else cfg.resolve(f"data/backtest_{args.market}.db")
    ctx = runner.make_context(bt_cfg, store, db, days[0], universe, notice_days=args.notice_days)
    from .fmt import inr, set_currency

    set_currency(bt_cfg.market.currency)
    if args.set:
        ctx.banner += " Research overrides (not the live settings): " + ", ".join(args.set) + "."
    print(f"Backtest: {len(universe)} {'NSE' if nse else 'US'} stocks, {days[0]} to {days[-1]} ({len(days)} days, "
          f"first {args.warmup} warm-up), capital {inr(bt_cfg.capital)}" + (f", overrides {args.set}" if args.set else ""),
          flush=True)
    run = runner.run_backtest(ctx, days, args.warmup,
                              progress=lambda i, n: print(f"  day {i}/{n}", end="\r", flush=True))
    print()
    print(backtest_summary(run))
    if args.report:
        from .reporting.weekly import write_report

        out = write_report(ctx, days[-1], days=(days[-1] - days[args.warmup]).days + 1,
                           out_dir=cfg.resolve(cfg.reporting.out_dir), name=f"backtest_{args.market}_{days[-1]}")
        print(f"report: {out}")
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

    sp = sub.add_parser("run", help="start the scheduler (paper mode by default)")
    sp.add_argument("--mock", action="store_true", help="use the synthetic market")
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("monitor", help="mark open spreads now and apply the exit rules")
    sp.add_argument("--phase", choices=["regular", "gap", "thesis"], default="regular")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=_cmd_monitor)

    sp = sub.add_parser("eod", help="run the end-of-day job now")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=_cmd_eod)

    sp = sub.add_parser("status", help="open positions, equity, kill switches")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=_cmd_status)

    sp = sub.add_parser("kill", help="stop new entries (exits keep running)")
    sp.add_argument("reason", nargs="?")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=_cmd_kill)

    sp = sub.add_parser("resume", help="clear the kill switch")
    sp.add_argument("--drawdown", action="store_true", help="also reset the drawdown pause")
    sp.add_argument("--mock", action="store_true")
    sp.set_defaults(func=_cmd_resume)

    sp = sub.add_parser("sim", help="end-to-end paper simulation on the synthetic market")
    sp.add_argument("--warmup", type=int, default=130, help="days of IV history to build first")
    sp.add_argument("--days", type=int, default=60, help="trading days to simulate")
    sp.add_argument("--capital", type=float, help="override capital for this run")
    sp.add_argument("--seed", type=int, help="synthetic market seed")
    sp.add_argument("--outages", type=float, default=0.0,
                    help="probability per day of a 2-hour outage (exercises missed-mark replay)")
    sp.add_argument("--db", help="database file for this run (default data/trading_mock.db)")
    sp.add_argument("--symbols", type=int, help="number of synthetic stocks (default broker.mock_symbols)")
    sp.set_defaults(func=_cmd_sim)

    sp = sub.add_parser("report", help="write the weekly HTML report now")
    sp.add_argument("--mock", action="store_true")
    sp.add_argument("--db", help="report on a specific database (e.g. a simulation run)")
    sp.add_argument("--asof", help="YYYY-MM-DD (default: today, or the last equity date with --db)")
    sp.set_defaults(func=_cmd_report)

    sp = sub.add_parser("backtest", help="replay real end-of-day US option data through the live code path")
    sp.add_argument("action", choices=["load", "run"], help="load: download US data from DoltHub; run: replay")
    sp.add_argument("--market", choices=["us", "nse"], default="us",
                    help="us: DoltHub data; nse: quotes recorded by this system while paper trading")
    sp.add_argument("--start", default="2024-01-02")
    sp.add_argument("--end", default="2026-10-30")
    sp.add_argument("--warmup", type=int, default=130, help="days of scanning without entries (IV history)")
    sp.add_argument("--capital", type=float, default=None, help="starting capital (US default $100,000; NSE: config)")
    sp.add_argument("--store", default=None, help="market store (default data/market_us.db or paths.market_store)")
    sp.add_argument("--universe", help="CSV of symbol,sector (default app/backtest/us_universe.csv)")
    sp.add_argument("--db", help="trading database for this run (default data/backtest_us.db)")
    sp.add_argument("--notice-days", type=int, default=14, help="days before an earnings date that it is known")
    sp.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="override a threshold for this run, e.g. --set iv.min_iv_hv_ratio=1.15")
    sp.add_argument("--report", action="store_true", help="write an HTML report for the trading period")
    sp.set_defaults(func=_cmd_backtest)

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
