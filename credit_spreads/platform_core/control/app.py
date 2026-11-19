"""The control centre (spec: Control centre (phone)). Serve it on the laptop at 127.0.0.1 and reach it
from the phone through Tailscale (`tailscale serve`), never the public internet; the PIN is a second
layer. Paper trading only: the order ticket fills through the shared paper broker.

Screens: Overview, Positions and funds, Order ticket (discretionary only), ROI scanner, Journal and
reports, and an upload page for the events file (plus sectors.csv and bhavcopy fallbacks).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fmt import inr

from .. import accounts, discretionary
from ..db import get_state, put_state
from ..models import CloseCheck, PTrade, ScreenerRow, SetupRow, Upload
from . import auth

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent / "templates"
LABELS = {"oi_tight": "OI-wall, tighter rules", "oi_loose": "OI-wall, looser rules", "level_strict": "levels, strict book",
          "level_research": "levels, research book", "discretionary": "owner's trades"}
MAX_UPLOAD = 5 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def _sign(x) -> str:
    return "pos" if (x or 0) > 0 else ("neg" if (x or 0) < 0 else "")


def sparkline(points, width: int = 320, height: int = 56) -> str:
    vals = [p["value"] for p in points or []]
    if len(vals) < 2:
        return '<div class="small mute">Equity curve after two recorded sessions.</div>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    xs = [i * (width - 4) / (len(vals) - 1) + 2 for i in range(len(vals))]
    ys = [height - 4 - (v - lo) / span * (height - 8) for v in vals]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    color = "var(--pos)" if vals[-1] >= vals[0] else "var(--neg)"
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="equity curve"><polyline fill="none" stroke="{color}" stroke-width="2" points="{path}"/></svg>')


def create_app(p, env: Optional[dict] = None) -> FastAPI:
    """`p`: a runtime.Platform. `env`: CONTROL_PIN_HASH and CONTROL_SECRET (defaults to the process's .env)."""
    if env is None:
        from app.env import load_env

        env = dict(load_env())
    pin_hash, secret = env.get("CONTROL_PIN_HASH", ""), env.get("CONTROL_SECRET", "")
    cfg = p.cfg
    jinja = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
    jinja.filters.update(inr=lambda x: inr(x or 0), pct=_pct, sign=_sign)
    jinja.globals.update(spark=sparkline)
    lockout = auth.Lockout(p.pengine)
    app = FastAPI(title="Control centre", docs_url=None, redoc_url=None, openapi_url=None)
    scan_lock = threading.Lock()

    def authed(request: Request) -> bool:
        if not cfg.control_centre.pin_required:
            return True
        return bool(pin_hash and secret) and auth.check(secret, request.cookies.get(auth.COOKIE))

    def page(request: Request, name: str, active: str = "", refresh: Optional[int] = None, **kw) -> HTMLResponse:
        today = p.today()
        html = jinja.get_template(name).render(
            title=kw.pop("title", name.split(".")[0].title()), now=p.now().strftime("%d %b %H:%M"),
            live=cfg.is_live(today), go_live=cfg.go_live_date, authed=authed(request), active=active,
            refresh=refresh, flash=request.query_params.get("msg"), **kw)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def need_login(request: Request):
        if not authed(request):
            if request.url.path.startswith("/api/"):
                raise HTTPException(401, "log in first")
            raise HTTPException(303, headers={"Location": "/login"})

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    def back(path: str, msg: str) -> RedirectResponse:
        from urllib.parse import quote

        sep = "&" if "?" in path else "?"
        return RedirectResponse(f"{path}{sep}msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ login
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        left = lockout.locked()
        return page(request, "login.html", title="Login", pin_set=bool(pin_hash and secret),
                    locked=int(left // 60) + 1 if left else 0)

    @app.post("/login")
    def login(request: Request, pin: str = Form(...)):
        if lockout.locked() or not (pin_hash and secret):
            return RedirectResponse("/login", status_code=303)
        if not auth.verify_pin(pin, pin_hash):
            lockout.fail()
            return back("/login", "Wrong PIN.")
        lockout.success()
        resp = RedirectResponse("/", status_code=303)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(auth.COOKIE, auth.sign(secret), max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=secure)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ------------------------------------------------------------------ overview, positions
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        need_login(request)
        return page(request, "overview.html", "/", cfg.control_centre.refresh_seconds, title="Overview",
                    accounts=accounts.overview(p.broker, p.today()), labels=LABELS, capital=cfg.capital)

    @app.get("/api/overview")
    def api_overview(request: Request):
        need_login(request)
        return JSONResponse(json.loads(json.dumps(accounts.overview(p.broker, p.today()), default=str)))

    @app.get("/positions", response_class=HTMLResponse)
    def positions(request: Request, account: str = "level_strict"):
        need_login(request)
        if account not in cfg.accounts:
            raise HTTPException(404, "no such account")
        return page(request, "positions.html", "/positions", cfg.control_centre.refresh_seconds, title="Positions",
                    account=account, account_names=cfg.accounts, positions=accounts.positions(p.broker, account),
                    funds=accounts.funds(p.pengine, account, 100), summary=p.broker.account_value(account))

    @app.get("/api/positions")
    def api_positions(request: Request, account: str):
        need_login(request)
        return JSONResponse(json.loads(json.dumps(accounts.positions(p.broker, account), default=str)))

    # ------------------------------------------------------------------ order ticket
    @app.get("/ticket", response_class=HTMLResponse)
    def ticket(request: Request, symbol: str = ""):
        need_login(request)
        try:
            stocks = p.ds.stocks()
        except Exception as exc:
            stocks = []
            log.warning("stock list unavailable: %s", exc)
        return page(request, "ticket.html", "/ticket", None, title="Order ticket", stocks=stocks, symbol=symbol.upper(),
                    tags=discretionary.TAGS, positions=accounts.positions(p.broker, discretionary.ACCOUNT),
                    started=p.broker.state(discretionary.ACCOUNT).started,
                    preview_refresh=cfg.control_centre.refresh_seconds)

    @app.get("/api/expiries")
    def api_expiries(request: Request, symbol: str):
        need_login(request)
        sym = symbol.strip().upper()
        exps = [e for e in p.ds.expiries(sym) if e >= p.today()]
        if not exps:
            raise HTTPException(404, f"{sym} has no listed expiries")
        chosen, _ = p.ds.expiry_for(sym, p.today())
        return {"symbol": sym, "expiries": [e.isoformat() for e in exps], "chosen": chosen.isoformat() if chosen else None}

    @app.get("/api/chain")
    def api_chain(request: Request, symbol: str, expiry: str):
        need_login(request)
        try:
            return JSONResponse(json.loads(json.dumps(discretionary.chain_view(p, symbol.strip().upper(),
                                                                               date.fromisoformat(expiry)), default=str)))
        except (LookupError, ValueError) as exc:
            raise HTTPException(404, str(exc))

    def _ticket_args(symbol, expiry, direction, short, long, lots, stop, tag):
        try:
            return dict(symbol=symbol.strip().upper(), expiry=date.fromisoformat(expiry), direction=direction,
                        short_k=float(short), long_k=float(long), lots=int(lots),
                        stop=float(stop) if str(stop).strip() else None, tag=tag or "none")
        except ValueError as exc:
            raise HTTPException(400, f"check the ticket: {exc}")

    @app.post("/api/preview")
    def api_preview(request: Request, symbol: str = Form(...), expiry: str = Form(...), direction: str = Form(...),
                    short: str = Form(...), long: str = Form(...), lots: str = Form("1"), stop: str = Form(""),
                    tag: str = Form("none"), reason: str = Form("")):
        need_login(request)
        try:
            pv = discretionary.preview(p, **_ticket_args(symbol, expiry, direction, short, long, lots, stop, tag))
        except (discretionary.TicketError, LookupError) as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse(json.loads(json.dumps(pv, default=str)))

    @app.post("/ticket/place")
    def place(request: Request, symbol: str = Form(...), expiry: str = Form(...), direction: str = Form(...),
              short: str = Form(...), long: str = Form(...), lots: str = Form("1"), stop: str = Form(""),
              tag: str = Form("none"), reason: str = Form("")):
        need_login(request)
        try:
            res = discretionary.place(p, **_ticket_args(symbol, expiry, direction, short, long, lots, stop, tag),
                                      reason=reason)
        except (discretionary.TicketError, LookupError) as exc:
            return back("/ticket", f"Not placed: {exc}")
        if res["ok"]:
            return back("/positions?account=discretionary", f"Filled: {symbol.upper()} credit {res['credit']:.2f}, "
                                                             f"margin {inr(res['margin'])}")
        return back("/ticket", f"Rejected: {res['reason']}")

    @app.post("/close/{trade_id}")
    def close(request: Request, trade_id: str):
        need_login(request)
        try:
            res = discretionary.close(p, trade_id)
        except discretionary.TicketError as exc:
            return back("/positions?account=discretionary", str(exc))
        if res["ok"]:
            return back("/positions?account=discretionary",
                        f"Closed: P&L {inr(res['pnl'])} ({(res['pnl_r'] or 0):+.2f}R)")
        return back("/positions?account=discretionary", f"Not closed: {res['reason']}")

    # ------------------------------------------------------------------ ROI scanner
    @app.get("/scanner", response_class=HTMLResponse)
    def scanner(request: Request, sector: str = "", setup: str = ""):
        need_login(request)
        live = get_state(p.pengine, "scanner_live")
        rows, source = [], ""
        if live and live.get("date") == p.today().isoformat():
            rows, source = live["rows"], f"Live refresh at {live['ts']}"
        else:
            with Session(p.pengine) as s:
                d = s.scalars(select(ScreenerRow.date).order_by(ScreenerRow.date.desc()).limit(1)).first()
                if d:
                    srows = s.scalars(select(ScreenerRow).where(ScreenerRow.date == d, ScreenerRow.rank.is_not(None))
                                      .order_by(ScreenerRow.rank)).all()
                    for r in srows:
                        det = json.loads(r.detail_json or "{}")
                        rows.append({"rank": r.rank, "symbol": r.symbol, "rom_call": r.rom_call, "rom_put": r.rom_put,
                                     "sector": det.get("sector"), "setups": det.get("setups", [])})
                    source = f"Screener of {d} (15:20)"
        sectors = sorted({r.get("sector") for r in rows if r.get("sector")})
        if sector:
            rows = [r for r in rows if r.get("sector") == sector]
        if setup == "any":
            rows = [r for r in rows if r.get("setups")]
        elif setup:
            rows = [r for r in rows if any(s.startswith(f"S{setup} ") or s.startswith(f"armed S{setup} ") for s in r.get("setups", []))]
        with Session(p.pengine) as s:
            st = s.scalars(select(SetupRow).where(SetupRow.state.in_(("armed", "confirming", "confirmed")))
                           .order_by(SetupRow.updated_at.desc()).limit(60)).all()
        setups = [{"symbol": x.symbol, "setup": x.setup, "direction": x.direction, "level": x.level, "state": x.state,
                   "route": x.route, "day0": x.day0_date, "strict_trade_id": x.strict_trade_id,
                   "research_trade_id": x.research_trade_id} for x in st
                  if x.state != "confirmed" or (x.last_session == p.today())]
        if setup and setup != "any":
            setups = [x for x in setups if str(x["setup"]) == setup]
        return page(request, "scanner.html", "/scanner", 5 if scan_lock.locked() else None, title="ROI scanner",
                    rows=rows[: max(cfg.screener.report_top_n, 50)], setups=setups, sectors=sectors, sector=sector,
                    setup=setup, source=source or "No screener run yet", running=scan_lock.locked())

    @app.post("/scanner/refresh")
    def scanner_refresh(request: Request):
        need_login(request)
        if scan_lock.locked():
            return back("/scanner", "A refresh is already running.")

        def run():
            with scan_lock:
                try:
                    rows = p.engine.scan_now()
                    put_state(p.pengine, "scanner_live", {"date": p.today().isoformat(),
                                                          "ts": p.now().strftime("%H:%M:%S"), "rows": rows}, p.now())
                except Exception as exc:
                    log.exception("scanner refresh failed")
                    put_state(p.pengine, "scanner_live_error", str(exc), p.now())

        threading.Thread(target=run, name="scanner-refresh", daemon=True).start()
        return back("/scanner", "Refreshing with live quotes; this takes a minute or two.")

    # ------------------------------------------------------------------ journal
    @app.get("/journal", response_class=HTMLResponse)
    def journal(request: Request):
        need_login(request)
        with Session(p.pengine) as s:
            ts = s.scalars(select(PTrade).where(PTrade.account == discretionary.ACCOUNT)
                           .order_by(PTrade.entry_ts.desc()).limit(200)).all()
        trades = []
        for t in ts:
            legs = json.loads(t.legs_json)
            trades.append({**{c: getattr(t, c) for c in ("id", "symbol", "direction", "expiry", "lots", "status", "exit_reason",
                                                         "credit", "margin", "rom", "one_r", "max_loss", "pnl_inr", "pnl_r",
                                                         "holding_days", "setup_tag", "owner_stop", "reason")},
                           "entry_ts": t.entry_ts.strftime("%d %b %H:%M"),
                           "short": next(lg["strike"] for lg in legs if lg["side"] == "SELL"),
                           "long": next(lg["strike"] for lg in legs if lg["side"] == "BUY"),
                           "ctx": json.loads(t.context_json or "{}")})
        return page(request, "journal.html", "/journal", None, title="Journal", trades=trades, tags=discretionary.TAGS)

    @app.post("/journal/{trade_id}")
    def journal_edit(request: Request, trade_id: str, tag: str = Form("none"), reason: str = Form(""), stop: str = Form("")):
        need_login(request)
        try:
            s = stop.strip()
            discretionary.edit(p, trade_id, tag=tag, reason=reason, stop=float(s) if s else None, clear_stop=not s)
        except (discretionary.TicketError, ValueError) as exc:
            return back("/journal", f"Not saved: {exc}")
        return back("/journal", "Saved.")

    # ------------------------------------------------------------------ reports
    reports_root = cfg.resolve(cfg.paths.reports)

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request):
        need_login(request)

        def listing(sub: str):
            folder = reports_root / sub
            return [f"{sub}/{x.name}" for x in sorted(folder.glob("*.html"), reverse=True)[:40]] if folder.exists() else []

        runs = []
        rdir = reports_root / "runs"
        if rdir.exists():
            for f in sorted(rdir.glob("*.json"), reverse=True)[:15]:
                try:
                    runs.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        with Session(p.pengine) as s:
            setups = s.scalars(select(SetupRow).order_by(SetupRow.updated_at.desc()).limit(40)).all()
            checks = s.scalars(select(CloseCheck).order_by(CloseCheck.id.desc()).limit(40)).all()
        return page(request, "reports.html", "/reports", None, title="Reports", daily=listing("daily"),
                    monthly=listing("monthly"), runs=runs, setups=setups, checks=checks)

    @app.get("/reports/file/{sub}/{name}")
    def report_file(request: Request, sub: str, name: str):
        need_login(request)
        if sub not in ("daily", "monthly") or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html", name):
            raise HTTPException(404, "not found")
        path = (reports_root / sub / name).resolve()
        if reports_root.resolve() not in path.parents or not path.exists():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="text/html")

    # ------------------------------------------------------------------ uploads
    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request):
        need_login(request)
        with Session(p.pengine) as s:
            ups = s.scalars(select(Upload).order_by(Upload.id.desc()).limit(20)).all()
        return page(request, "upload.html", "/upload", None, title="Upload", uploads=ups,
                    events_age=p.ds.newest_events_file_age_days(), max_age=cfg.data.events_file_max_age_days,
                    staleness=cfg.screener.event_staleness_days)

    @app.post("/upload")
    async def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
        need_login(request)
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return back("/upload", "File too large (limit 5 MB).")
        name = SAFE_NAME.sub("_", Path(file.filename or "upload").name)[:120] or "upload"
        try:
            rows = save_upload(p, kind, name, data)
        except ValueError as exc:
            return back("/upload", f"Not saved: {exc}")
        with Session(p.pengine) as s:
            s.add(Upload(ts=p.now(), kind=kind, filename=name, rows=rows))
            s.commit()
        return back("/upload", f"Saved {name}" + (f": {rows} rows loaded." if rows is not None else "."))

    return app


def save_upload(p, kind: str, name: str, data: bytes) -> Optional[int]:
    """Store an uploaded file where the platform reads it; returns rows loaded where that applies."""
    lower = name.lower()
    ctx = p.ctx
    if kind == "events":
        if not lower.endswith(".csv"):
            raise ValueError("the events file must be a .csv export")
        text = data.decode("utf-8", errors="replace")
        if "," not in text.split("\n", 1)[0]:
            raise ValueError("that does not look like a CSV file")
        folder = p.ds.events_import_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        from app.data import events as ev

        return ev.load_imports(ctx)
    if kind == "sectors":
        import csv
        import io

        if not lower.endswith(".csv"):
            raise ValueError("sectors must be a .csv with columns symbol and sector")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))
        cols = {c.strip().lower() for c in (reader.fieldnames or [])}
        if not ({"symbol"} <= cols and ({"sector"} & cols or {"industry"} & cols)):
            raise ValueError("sectors.csv needs a symbol column and a sector (or industry) column")
        n = sum(1 for _ in reader)
        p.cfg.resolve(p.cfg.paths.sectors_file).write_bytes(data)
        return n
    if kind == "bhavcopy":
        if not (lower.endswith(".csv") or lower.endswith(".zip")):
            raise ValueError("a bhavcopy is NSE's .zip or .csv")
        if not re.search(r"(FO|CM)_.*\d{8}", name.upper()):
            raise ValueError("keep NSE's file name (it carries the market and the date)")
        folder = ctx.cfg.resolve(ctx.cfg.paths.imports) / "bhavcopy"
        folder.mkdir(parents=True, exist_ok=True)
        from ..nse_files import _unzip

        _unzip(data)                                   # rejects web pages and unreadable files
        (folder / name).write_bytes(data)
        return None
    raise ValueError(f"unknown upload kind {kind!r}")
