"""Small dependency-free SVG/HTML charts for the weekly report.

Marks follow one spec: 2px lines with a 10% area wash, bars at most 24px thick with a 4px
rounded data end, hairline recessive gridlines, a single y-axis, text in text tokens (never the
series colour), and a native hover tooltip (<title>) on every mark. Colours are CSS variables
defined by the report template for light and dark mode.
"""

from __future__ import annotations

import html
import math
from datetime import date
from typing import Callable, List, Optional, Sequence, Tuple


def _nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 10))
        v += step
    return ticks


def compact(v: float) -> str:
    a = abs(v)
    if a >= 1e7:
        return f"{v / 1e7:.1f}Cr"
    if a >= 1e5:
        return f"{v / 1e5:.1f}L"
    if a >= 1e4:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"


def line_chart(points: Sequence[Tuple[date, float]], fmt: Callable[[float], str] = compact,
               width: int = 720, height: int = 220, color: str = "var(--series-1)", invert: bool = False,
               label: str = "") -> str:
    """Single-series line with area wash. `invert` draws values downward from zero (drawdown)."""
    if not points:
        return '<p class="muted">No data yet.</p>'
    pl, pr, pt, pb = 56, 64, 12, 28
    w, h = width - pl - pr, height - pt - pb
    ys = [v for _, v in points]
    lo, hi = (min(ys + [0.0]), max(ys + [0.0])) if invert else (min(ys), max(ys))
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    n = len(points)
    def x(i): return pl + (w * i / (n - 1) if n > 1 else w / 2)
    def y(v): return pt + h * (hi - v) / (hi - lo)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}" class="chart">']
    for t in ticks:
        parts.append(f'<line x1="{pl}" x2="{pl + w}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                     f'<text x="{pl - 8}" y="{y(t) + 4:.1f}" class="tick" text-anchor="end">{fmt(t)}</text>')
    for i in sorted({0, n // 2, n - 1}):
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" class="tick" text-anchor="middle">'
                     f'{points[i][0]:%d %b}</text>')
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))
    base = y(0.0) if invert or lo <= 0 <= hi else pt + h
    parts.append(f'<path d="{path} L{x(n - 1):.1f},{base:.1f} L{x(0):.1f},{base:.1f} Z" fill="{color}" '
                 f'opacity="0.10" stroke="none"/>')
    parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" '
                 f'stroke-linecap="round"/>')
    lx, ly = x(n - 1), y(points[-1][1])
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="{color}" stroke="var(--surface-1)" stroke-width="2"/>'
                 f'<text x="{lx + 8:.1f}" y="{ly + 4:.1f}" class="value">{fmt(points[-1][1])}</text>')
    step = w / max(n - 1, 1)
    for i, (d, v) in enumerate(points):
        parts.append(f'<rect x="{x(i) - step / 2:.1f}" y="{pt}" width="{max(step, 2):.1f}" height="{h}" class="hit">'
                     f'<title>{d:%d %b %Y}: {html.escape(fmt(v))}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def bar_rows(items: Sequence[Tuple[str, float]], fmt: Callable[[float], str] = compact, diverging: bool = False,
             note: Callable[[int], str] = lambda i: "") -> str:
    """Horizontal bars as HTML rows. Diverging: positive to the right in the cool pole, negative to
    the left in the warm pole, from a shared zero baseline."""
    if not items:
        return '<p class="muted">No data yet.</p>'
    m = max((abs(v) for _, v in items), default=0) or 1
    rows = []
    for i, (lab, v) in enumerate(items):
        pct = abs(v) / m * (50 if diverging else 100)
        cls = "neg" if v < 0 else "pos"
        tip = html.escape(f"{lab}: {fmt(v)}{note(i)}")
        if diverging:
            bar = (f'<div class="track div"><span class="zero"></span>'
                   f'<span class="bar {cls}" style="width:{pct:.1f}%;{"right:50%" if v < 0 else "left:50%"}"'
                   f' title="{tip}"></span></div>')
        else:
            bar = f'<div class="track"><span class="bar pos" style="width:{pct:.1f}%" title="{tip}"></span></div>'
        rows.append(f'<div class="brow"><span class="blab">{html.escape(lab)}</span>{bar}'
                    f'<span class="bval">{html.escape(fmt(v))}{html.escape(note(i))}</span></div>')
    return '<div class="bars">' + "".join(rows) + "</div>"


def histogram(values: Sequence[float], edges: Sequence[float], width: int = 720, height: int = 200,
              xlabel: Callable[[float, float], str] = lambda a, b: f"{a:g}-{b:g}") -> str:
    if not values:
        return '<p class="muted">No closed trades yet.</p>'
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(edges) - 1):
            last = i == len(edges) - 2
            if edges[i] <= v < edges[i + 1] or (last and v >= edges[i]):
                counts[i] += 1
                break
    pl, pr, pt, pb = 40, 12, 16, 32
    w, h = width - pl - pr, height - pt - pb
    top = max(counts) or 1
    ticks = _nice_ticks(0, top, 3)
    top = max(top, ticks[-1])
    band = w / len(counts)
    bw = min(24, band - 2)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="histogram" class="chart">']
    for t in ticks:
        yy = pt + h * (1 - t / top)
        parts.append(f'<line x1="{pl}" x2="{pl + w}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
                     f'<text x="{pl - 6}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{t:g}</text>')
    for i, c in enumerate(counts):
        cx = pl + band * i + band / 2
        bh = h * c / top
        label = xlabel(edges[i], edges[i + 1]) if i < len(counts) - 1 else f">= {edges[i]:g}"
        if c:
            r = min(4, bh / 2, bw / 2)
            x0, y0 = cx - bw / 2, pt + h - bh
            parts.append(f'<path d="M{x0:.1f},{pt + h:.1f} V{y0 + r:.1f} Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} '
                         f'H{x0 + bw - r:.1f} Q{x0 + bw:.1f},{y0:.1f} {x0 + bw:.1f},{y0 + r:.1f} V{pt + h:.1f} Z" '
                         f'fill="var(--series-1)"><title>{html.escape(label)}x credit: {c} trade(s)</title></path>'
                         f'<text x="{cx:.1f}" y="{y0 - 4:.1f}" class="value" text-anchor="middle">{c}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{height - 12}" class="tick" text-anchor="middle">{html.escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def calibration_chart(rows: Sequence[dict], width: int = 360, height: int = 300) -> str:
    if not rows:
        return '<p class="muted">Needs closed trades.</p>'
    p = 48
    s = width - 2 * p
    def X(v): return p + s * v
    def Y(v): return height - p - (height - 2 * p) * v
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="calibration" class="chart">']
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{p}" x2="{width - p}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" class="grid"/>'
                     f'<text x="{p - 6}" y="{Y(t) + 4:.1f}" class="tick" text-anchor="end">{t:.0%}</text>'
                     f'<text x="{X(t):.1f}" y="{height - p + 16}" class="tick" text-anchor="middle">{t:.0%}</text>')
    parts.append(f'<line x1="{X(0)}" y1="{Y(0)}" x2="{X(1)}" y2="{Y(1)}" class="ref"/>'
                 f'<text x="{X(0.98):.1f}" y="{Y(0.98) + 14:.1f}" class="tick" text-anchor="end">perfect calibration</text>')
    for r in rows:
        parts.append(f'<circle cx="{X(r["p_model"]):.1f}" cy="{Y(r["actual"]):.1f}" r="5" fill="var(--series-1)" '
                     f'stroke="var(--surface-1)" stroke-width="2"><title>bin {r["bin"]}: p_model '
                     f'{r["p_model"]:.0%}, actual {r["actual"]:.0%} (n={r["n"]})</title></circle>')
    parts.append(f'<text x="{width / 2}" y="{height - 4}" class="tick" text-anchor="middle">p_model</text>'
                 f'<text x="10" y="{height / 2}" class="tick" transform="rotate(-90 10 {height / 2})" '
                 f'text-anchor="middle">actual win rate</text></svg>')
    return "".join(parts)
