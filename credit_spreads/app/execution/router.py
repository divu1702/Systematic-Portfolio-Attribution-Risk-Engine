"""Order-routing adapter selected by `mode`. Strategy, risk and monitor code are shared; only
this adapter differs between paper and live."""

from __future__ import annotations

from ..safety import require_live
from .paper import PaperExecutor


def get_executor(ctx):
    if ctx.cfg.mode == "paper":
        return PaperExecutor(ctx)
    # Live: both the config and the manual confirmation flag must allow it.
    require_live(ctx.cfg, ctx.env)
    raise NotImplementedError("the live order adapter is milestone 8 and has not been built")
