"""Order-routing adapter selected by `mode`. Strategy, risk and monitor code are shared; only
this adapter differs between paper and live."""

from __future__ import annotations

from ..safety import require_live
from .paper import PaperExecutor


def get_executor(ctx):
    """The order adapter for this context's book. Paper sleeves (config: strategies) are always paper.
    On the platform (platform_core/oi_bridge.py), mapped books fill through the shared paper broker."""
    factory = ctx.cache.get("platform_executor")
    if factory is not None:
        ex = factory(ctx)
        if ex is not None:
            return ex
    if ctx.cfg.mode == "paper" or ctx.strategy != "main":
        return PaperExecutor(ctx)
    # Live: both the config and the manual confirmation flag must allow it.
    require_live(ctx.cfg, ctx.env)
    raise NotImplementedError("the live order adapter is milestone 8 and has not been built")
