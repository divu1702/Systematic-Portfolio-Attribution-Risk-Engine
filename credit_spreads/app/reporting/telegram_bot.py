"""Telegram alerts and commands.

TelegramNotifier sends every alert (entries, exits, stops, rejections, reconciliation
mismatches, failed logins) to the owner's chat, and always logs it too.

Commands (only from TELEGRAM_CHAT_ID; anyone else is ignored):
  /status              positions, equity, kill switches
  /kill [reason]       stop new entries (exits keep running; live mode also cancels open orders)
  /resume              clear the kill switch          /resume_drawdown  reset the drawdown pause
  /login               today's Kite login link        /token <request_token or redirect URL>
  /pending  /approve N  /reject N                     trade proposals (live mode)
  /report              build the weekly report now    /help
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .notifier import LogNotifier, Notifier

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
KIND_PREFIX = {"entry": "ENTRY", "exit": "", "risk": "RISK", "rejection": "", "login": "LOGIN", "error": "ERROR",
               "warning": "WARN", "proposal": "", "reconcile": "RECONCILE"}


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, fallback: Optional[LogNotifier] = None, session=None,
                 timeout: float = 10.0):
        import requests

        self.token, self.chat_id, self.timeout = token, str(chat_id), timeout
        self.fallback = fallback or LogNotifier()
        self.s = session or requests.Session()
        self.failures = 0

    @property
    def sent(self):
        return self.fallback.sent

    def send(self, text: str, kind: str = "info") -> None:
        self.fallback.send(text, kind)
        prefix = KIND_PREFIX.get(kind, kind.upper())
        body = text if not prefix or text.startswith(prefix) else f"{prefix}: {text}"
        try:
            r = self.s.post(API.format(token=self.token, method="sendMessage"), timeout=self.timeout,
                            json={"chat_id": self.chat_id, "text": body[:4000], "disable_web_page_preview": True})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            self.failures = 0
        except Exception as exc:   # alerts must never crash a job
            self.failures += 1
            log.warning("Telegram send failed (%s); alert kept in the log", exc)


HELP = __doc__.split("Commands", 1)[1].split("\n", 1)[1]


def handle_command(ctx, text: str, chat_id: str, report_fn: Optional[Callable] = None) -> Optional[str]:
    """Pure command handler (testable without Telegram). Returns the reply, or None to ignore."""
    from ..data import state
    from ..views import status_report
    from . import approvals

    allowed = str(ctx.env.get("TELEGRAM_CHAT_ID") or "")
    if not allowed or str(chat_id) != allowed:
        log.warning("ignored Telegram message from chat %s", chat_id)
        return None
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    cmd, arg = parts[0].split("@")[0].lower(), (parts[1] if len(parts) > 1 else "")
    if cmd in ("/start", "/help"):
        return "Commands:\n" + HELP
    if cmd == "/status":
        return status_report(ctx)
    if cmd == "/kill":
        state.put(ctx.engine, state.KILLED, arg or "Telegram /kill", ts=ctx.clock.now())
        extra = " Open orders cancelled." if ctx.cfg.mode == "live" else ""
        return f"Kill switch ON: no new entries. Exits keep running.{extra} /resume to undo."
    if cmd == "/resume":
        state.clear(ctx.engine, state.KILLED)
        return "Kill switch cleared. (Drawdown pause, if any, needs /resume_drawdown.)"
    if cmd == "/resume_drawdown":
        state.clear(ctx.engine, state.DD_PAUSED)
        return "Drawdown pause reset. New entries allowed if no other switch is on."
    if cmd == "/login":
        return f"Log in and complete 2FA: {ctx.broker.login_url()}\nThen send /token <request_token>."
    if cmd == "/token":
        if not arg:
            return "Usage: /token <request_token or the full redirect URL>"
        try:
            ctx.broker.complete_login(arg)
        except Exception as exc:
            ctx.notifier.send(f"Kite login failed: {exc}", "login")
            return f"Login failed: {exc}"
        return "Kite session saved for today."
    if cmd == "/pending":
        ps = approvals.pending(ctx)
        return "\n".join(f"#{p.id} {p.summary}" for p in ps) if ps else "No pending proposals."
    if cmd in ("/approve", "/reject"):
        try:
            pid = int(arg.strip().lstrip("#"))
        except ValueError:
            return f"Usage: {cmd} <proposal number>"
        return approvals.decide(ctx, pid, cmd == "/approve")[1]
    if cmd == "/report":
        if report_fn is None:
            from .weekly import write_report

            report_fn = write_report
        return f"Report written: {report_fn(ctx)}"
    return "Unknown command. /help lists them."


def run_bot(ctx) -> None:  # pragma: no cover - needs the network
    """Long-poll Telegram in the foreground (the scheduler runs in a background thread)."""
    import asyncio

    from telegram import Update
    from telegram.ext import Application, MessageHandler, filters

    async def on_command(update: Update, _context) -> None:
        if not update.message:
            return
        reply = await asyncio.to_thread(handle_command, ctx, update.message.text, str(update.effective_chat.id))
        if reply:
            await update.message.reply_text(reply[:4000])

    app = Application.builder().token(ctx.env["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(MessageHandler(filters.COMMAND, on_command))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
