"""Charts as plain SVG - no plotting library is available in this environment.

Why hand-rolled SVG rather than a charting package: the sandbox has no network,
``matplotlib`` is absent and cannot be installed. SVG is a text format, so a
300-line emitter covers what the report needs (multi-series line charts, bar
charts, a heat map) and the output embeds directly in a Word document or prints
cleanly to PDF. ``XlsxWriter`` native charts cover the interactive review copy.

Every chart helper takes plain pandas objects and returns the SVG source as a
string; ``save`` writes it with a UTF-8 header.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

FONT = "Segoe UI, Helvetica, Arial, sans-serif"
PALETTE = ["#1f4e79", "#c00000", "#2e7d32", "#b26a00", "#6a1b9a", "#00838f"]


def _scale(values, lo: float, hi: float, out_lo: float, out_hi: float) -> np.ndarray:
    span = hi - lo if hi > lo else 1.0
    return out_lo + (np.asarray(values, dtype=float) - lo) / span * (out_hi - out_lo)


def _nice_bounds(values) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    lo, hi = float(np.nanmin(values)), float(np.nanmax(values))
    if not math.isfinite(lo) or not math.isfinite(hi):
        return 0.0, 1.0
    if lo == hi:
        pad = abs(lo) * 0.1 or 1.0
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def line_chart(
    frame: pd.DataFrame,
    x: str,
    series: dict[str, str],
    *,
    title: str,
    ylabel: str,
    width: int = 900,
    height: int = 420,
    percent: bool = False,
    markers: dict[str, str] | None = None,
) -> str:
    """Multi-series line chart; ``series`` maps column -> legend label."""
    left, right, top, bottom = 78, 210, 46, 62
    plot_w, plot_h = width - left - right, height - top - bottom
    xs = list(frame[x].astype(str))
    all_values = np.concatenate([frame[c].to_numpy(dtype=float) for c in series])
    y_lo, y_hi = _nice_bounds(all_values)
    if percent:
        y_lo = min(y_lo, 0.0)

    def x_at(i: int) -> float:
        return left + (plot_w * i / max(1, len(xs) - 1))

    def y_at(v: float) -> float:
        return top + plot_h - (plot_h * (v - y_lo) / (y_hi - y_lo))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{left}" y="26" font-family="{FONT}" font-size="17" font-weight="600" '
             f'fill="#1a1a1a">{title}</text>']
    # y grid
    for k in range(6):
        v = y_lo + (y_hi - y_lo) * k / 5
        y = y_at(v)
        label = f"{v*100:.1f}%" if percent else (f"{v/1e6:,.0f}m" if abs(v) >= 1e6 else f"{v:,.2f}")
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#e6e6e6"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-family="{FONT}" '
                     f'font-size="11" fill="#555">{label}</text>')
    # x labels
    step = max(1, len(xs) // 12)
    for i in range(0, len(xs), step):
        parts.append(f'<text x="{x_at(i):.1f}" y="{top+plot_h+20}" text-anchor="middle" '
                     f'font-family="{FONT}" font-size="10" fill="#555">{xs[i]}</text>')
    parts.append(f'<text x="{left+plot_w/2}" y="{height-14}" text-anchor="middle" '
                 f'font-family="{FONT}" font-size="12" fill="#333">{x}</text>')
    parts.append(f'<text x="18" y="{top+plot_h/2}" transform="rotate(-90 18 {top+plot_h/2})" '
                 f'text-anchor="middle" font-family="{FONT}" font-size="12" fill="#333">{ylabel}</text>')
    # series
    for colour, (column, label) in zip(PALETTE, series.items()):
        values = frame[column].to_numpy(dtype=float)
        path = " ".join(f"{'M' if i == 0 else 'L'}{x_at(i):.1f},{y_at(v):.1f}"
                        for i, v in enumerate(values) if math.isfinite(v))
        parts.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2.1"/>')
    legend_x = left + plot_w + 22
    for i, (colour, (column, label)) in enumerate(zip(PALETTE, series.items())):
        y = top + 6 + i * 22
        parts.append(f'<rect x="{legend_x}" y="{y}" width="12" height="12" fill="{colour}"/>')
        parts.append(f'<text x="{legend_x+18}" y="{y+11}" font-family="{FONT}" font-size="11" '
                     f'fill="#333">{label}</text>')
    for period, text in (markers or {}).items():
        if period in xs:
            x = x_at(xs.index(period))
            parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}" '
                         f'stroke="#999" stroke-dasharray="4 3"/>')
            parts.append(f'<text x="{x+4:.1f}" y="{top+14}" font-family="{FONT}" font-size="10" '
                         f'fill="#666">{text}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def bar_chart(labels: list[str], values: dict[str, list[float]], *, title: str, ylabel: str,
              width: int = 900, height: int = 400, percent: bool = False) -> str:
    left, right, top, bottom = 78, 210, 46, 62
    plot_w, plot_h = width - left - right, height - top - bottom
    all_values = np.concatenate([np.asarray(v, dtype=float) for v in values.values()])
    y_lo, y_hi = _nice_bounds(all_values)
    y_lo = min(y_lo, 0.0)
    n = len(labels)
    groups = len(values)
    slot = plot_w / max(1, n)
    bar_w = max(3.0, slot * 0.78 / groups)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{left}" y="26" font-family="{FONT}" font-size="17" font-weight="600" '
             f'fill="#1a1a1a">{title}</text>']

    def y_at(v: float) -> float:
        return top + plot_h - (plot_h * (v - y_lo) / (y_hi - y_lo))

    for k in range(6):
        v = y_lo + (y_hi - y_lo) * k / 5
        y = y_at(v)
        label = f"{v*100:.1f}%" if percent else (f"{v/1e6:,.0f}m" if abs(v) >= 1e6 else f"{v:,.2f}")
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#e6e6e6"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-family="{FONT}" '
                     f'font-size="11" fill="#555">{label}</text>')
    for i, label in enumerate(labels):
        parts.append(f'<text x="{left+slot*(i+0.5):.1f}" y="{top+plot_h+20}" text-anchor="middle" '
                     f'font-family="{FONT}" font-size="11" fill="#555">{label}</text>')
    for g, (colour, (name, vals)) in enumerate(zip(PALETTE, values.items())):
        for i, v in enumerate(vals):
            if not math.isfinite(v):
                continue
            x = left + slot * (i + 0.5) - (groups * bar_w) / 2 + g * bar_w
            y0, y1 = y_at(0.0), y_at(v)
            parts.append(f'<rect x="{x:.1f}" y="{min(y0,y1):.1f}" width="{bar_w*0.92:.1f}" '
                         f'height="{abs(y1-y0):.1f}" fill="{colour}"/>')
        y = top + 6 + g * 22
        parts.append(f'<rect x="{left+plot_w+22}" y="{y}" width="12" height="12" fill="{colour}"/>')
        parts.append(f'<text x="{left+plot_w+40}" y="{y+11}" font-family="{FONT}" font-size="11" '
                     f'fill="#333">{name}</text>')
    parts.append(f'<text x="18" y="{top+plot_h/2}" transform="rotate(-90 18 {top+plot_h/2})" '
                 f'text-anchor="middle" font-family="{FONT}" font-size="12" fill="#333">{ylabel}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def heatmap(
    frame: pd.DataFrame,
    *,
    row: str,
    column: str,
    value: str,
    title: str,
    fmt: str = "{:,.2f}",
    width: int = 760,
    height: int = 470,
    unit_note: str = "",
) -> str:
    """Row x column grid shaded by ``value`` (dark = low)."""
    rows = sorted(frame[row].unique())
    cols = sorted(frame[column].unique())
    table = frame.pivot(index=row, columns=column, values=value).reindex(index=rows, columns=cols)
    values = table.to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    left, top = 96, 74
    cell_w = (width - left - 40) / max(1, len(cols))
    cell_h = (height - top - 70) / max(1, len(rows))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{left}" y="30" font-family="{FONT}" font-size="17" font-weight="600" '
             f'fill="#1a1a1a">{title}</text>']
    if unit_note:
        parts.append(f'<text x="{left}" y="50" font-family="{FONT}" font-size="11" fill="#666">{unit_note}</text>')
    for j, c in enumerate(cols):
        parts.append(f'<text x="{left+cell_w*(j+0.5):.1f}" y="{top-8}" text-anchor="middle" '
                     f'font-family="{FONT}" font-size="11" fill="#444">{c}</text>')
    for i, r in enumerate(rows):
        parts.append(f'<text x="{left-10}" y="{top+cell_h*(i+0.5)+4:.1f}" text-anchor="end" '
                     f'font-family="{FONT}" font-size="11" fill="#444">{r}</text>')
        for j, c in enumerate(cols):
            v = values[i, j]
            if not math.isfinite(v):
                fill, text = "#f2f2f2", "n/a"
            else:
                share = 0.0 if hi == lo else (v - lo) / (hi - lo)
                shade = int(238 - 178 * share)
                fill = f"rgb({shade},{shade+8},{min(255,shade+26)})"
                text = fmt.format(v)
            x, y = left + cell_w * j, top + cell_h * i
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" '
                         f'fill="{fill}" stroke="#ffffff" stroke-width="1.5"/>')
            parts.append(f'<text x="{x+cell_w/2:.1f}" y="{y+cell_h/2+4:.1f}" text-anchor="middle" '
                         f'font-family="{FONT}" font-size="10.5" fill="#111">{text}</text>')
    parts.append(f'<text x="{left}" y="{height-26}" font-family="{FONT}" font-size="11" fill="#666">'
                 f'rows: {row}　columns: {column}　darker = lower</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def save(svg: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return path
