"""Matplotlib rendering for per-OCH PPE traces.

Reusable between the `ppe_demo.py` CLI and the Flask server's
`/api/incidents/<sid>/ppe.png` endpoint.  Produces one subplot per dropping
OCh — the **end-to-end** profile over the cumulative route distance, with:

  * baseline (grey dashed) and observed samples (red) — the sawtooth,
  * faint vertical lines at each OMS span boundary (so the operator can read
    "which OMS"),
  * NaN regions (beyond a hard cut) shaded,
  * root anomalies marked at their cumulative `z_km`.
"""
from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")                # headless backend; safe in Flask
import matplotlib.pyplot as plt
import numpy as np

from .ppe import PpeTrace


def render(traces: list[PpeTrace], title: str, *, dpi: int = 130):
    """Render the figure to a matplotlib Figure (caller owns lifecycle)."""
    if not traces:
        fig, ax = plt.subplots(figsize=(6, 2.5), dpi=dpi)
        ax.text(0.5, 0.5, "no PPE traces for this incident",
                transform=ax.transAxes, ha="center", va="center",
                color="#888", fontsize=12)
        ax.axis("off")
        fig.suptitle(title, fontsize=12)
        return fig

    # One subplot per OCh trace (per dropping receiver).
    n = len(traces)
    n_cols = min(2, n)
    n_rows = (n + n_cols - 1) // n_cols
    fig, axes = plt.subplots(
        n_rows, n_cols,
        figsize=(6.0 * n_cols, 3.0 * n_rows),
        squeeze=False, sharey=False,
    )
    fig.suptitle(f"PPE traces — {title}", fontsize=13, y=0.995)
    flat = [ax for row in axes for ax in row]

    for ax, tr in zip(flat, traces):
        n_pts = len(tr.baseline) if len(tr.baseline) else len(tr.samples)
        z_km = np.arange(n_pts) * (tr.delta_z_m / 1000.0)
        if len(tr.baseline):
            ax.plot(z_km, tr.baseline, color="0.6", lw=1.2, ls="--",
                    label="baseline $P_0(z)$")

        # OMS span-boundary guides + labels.
        for i, sid in enumerate(tr.span_ids):
            start = tr.z_offsets[i] if i < len(tr.z_offsets) else 0.0
            ax.axvline(start, color="0.85", lw=0.8, zorder=0)
            label = sid.replace("span_", "")
            ax.text(start, 0.02, f" {label}", transform=ax.get_xaxis_transform(),
                    fontsize=7, color="0.5", rotation=90, va="bottom")

        if tr.degraded:
            ax.text(0.5, 0.5, "DEGRADED\n(no light)",
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=11, color="#aa0000",
                    bbox=dict(facecolor="#ffe5e5", edgecolor="#aa0000",
                              boxstyle="round,pad=0.3"))
        else:
            ax.plot(z_km, tr.samples, color="#c93030", lw=0.9,
                    label="observed $\\hat{P}(z)$")
            nan_mask = np.isnan(tr.samples)
            if nan_mask.any():
                first_nan = int(np.argmax(nan_mask))
                ax.axvspan(z_km[first_nan], z_km[-1],
                           color="#000000", alpha=0.05, lw=0)
                ax.text(z_km[first_nan], ax.get_ylim()[0] + 1.0, "  no light",
                        color="0.3", fontsize=8, va="bottom")

        for a in tr.anomalies:
            if not a.is_root:
                continue
            if a.kind == "step" and a.z_km is not None:
                ax.axvline(a.z_km, color="#c93030", lw=1.4, alpha=0.7)
                ax.text(a.z_km, ax.get_ylim()[1], f"  @{a.z_km:.1f}km",
                        color="#c93030", fontsize=8, va="top")
            elif a.kind == "slope" and a.z_km is not None:
                ax.axvspan(a.z_km, a.z_end_km or a.z_km,
                           color="#c93030", alpha=0.12, lw=0)
                ax.text((a.z_km + (a.z_end_km or a.z_km)) / 2, ax.get_ylim()[1],
                        f"  slope {a.magnitude_db:+.2f} dB/km",
                        color="#c93030", fontsize=8, va="top", ha="center")
            elif a.kind == "rx_offset":
                ax.axvline(z_km[-1], color="#c93030", lw=1.4, alpha=0.7)
                ax.text(z_km[-1], ax.get_ylim()[1], f" rx {a.magnitude_db:+.1f} dB",
                        color="#c93030", fontsize=8, va="top", ha="right")
            elif a.kind == "null":
                ax.text(0.5, 0.18, "(planted: null at z=0)",
                        transform=ax.transAxes, ha="center", fontsize=8,
                        color="#aa0000")

        ax.set_title(f"{tr.och}  →  {tr.drop_ne}   "
                     f"(L={tr.total_length_km:.0f} km)", fontsize=10)
        ax.set_xlabel("path distance z (km)")
        ax.set_ylabel("power (dBm)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="lower left", fontsize=8, framealpha=0.9)

    for ax in flat[len(traces):]:
        ax.axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def render_png(traces: list[PpeTrace], title: str, *, dpi: int = 130) -> bytes:
    """Render and return PNG image bytes.  Closes the figure after writing."""
    fig = render(traces, title, dpi=dpi)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    plt.close(fig)
    return buf.getvalue()


def render_to_file(traces: list[PpeTrace], title: str, out_path: str,
                   *, dpi: int = 130) -> None:
    """Render to a PNG file.  Used by `ppe_demo.py`."""
    fig = render(traces, title, dpi=dpi)
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
