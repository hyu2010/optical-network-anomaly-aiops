"""Power Profile Estimation (PPE) generator — per-OCH, end-to-end (Design/00 §4).

A coherent OTU receiver, while demodulating its channel, runs Digital
Back-Propagation and reconstructs the optical power as a function of distance,
``P(z)``, all the way back along its **own optical path** to the transmitter.
So the right unit of observation is **one trace per dropping OCh**, over the
*cumulative* distance of the whole route — not a per-span fragment.

For an OCh A→B→C the receiver at C produces a single curve over
``0 .. L_total`` (``L_total = Σ length(span) over the ERO``), sampled every
``Δz`` (default 200 m).  The baseline is an end-to-end walk of the ERO:

    P(z) = P_launch
           − α(z)·(distance in fibre)              (per-fibre-type attenuation)
           − Σ connector/splice losses · 1[z ≥ z_i]
           + Σ G_edfa,j · 1[z ≥ z_amp,j]           (EDFA gains — the sawtooth)
           + ε(z)                                   (DBP noise, σ grows with z)

The amplifier set is the union of the **inter-OMS** node OAs at each transited
NE and the **intra-OMS** inline EDFAs at every physical-span boundary (decision
D-4): one inline EDFA per ``span_count`` physical span, the last of which is the
node OA.  Each EDFA only partially compensates the preceding span's loss (a
small under-compensation margin), so the curve trends gently downward — exactly
the sawtooth in ``Promts/PPE_Data.png``.

Anomalies are reported in **path coordinates** (cumulative ``z_km``) plus a
reverse map to ``(span_id, offset_in_span_km)`` so ``knowledge_gateway`` can say
"which OMS, and where".  The per-fault δP shapes (step / slope / localised step
/ rx offset / NaN tail / degraded sentinel) carry over from the previous design;
they now apply to the single end-to-end curve at the correct cumulative ``z``.

Every emitter is paired one-for-one with the alarm rules in ``faults.py``; the
public ``generate(t, fault_type, *args)`` dispatcher mirrors ``faults.trigger``.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np

from .topology import Board, Link, Topology, Tunnel

# ---------------------------------------------------------------------------
# Composer constants (Design/00 §4.3, decision D-4)
# ---------------------------------------------------------------------------

DEFAULT_DELTA_Z_M = 200
#: Nominal per-physical-span connector loss baked into the auto-derived EDFA
#: gain (so a transparent line system compensates fibre + a connector).
DEFAULT_SPAN_CONNECTOR_DB = 0.3
#: Each EDFA leaves this much loss un-compensated, giving the gentle downward
#: trend across the sawtooth.
UNDER_COMP_MARGIN_DB = 0.4
#: ASE-noise extension (Design/12 §6.1 — `nfms:edfa-noise-figure-db` was
#: "reserved for σ(z) extension").  Each node OA the OCh route transits raises
#: the downstream DBP-noise floor by ``ASE_SIGMA_COEFF_DB × F``, where the
#: linear noise factor ``F = 10**(NF_dB/10)`` (NF = 5 dB → F ≈ 3.16 → ≈ 0.02 dB
#: of added σ per amplifier).  Gain is deliberately NOT taken from inventory —
#: the topology `nfms:amplifiers` own the sawtooth; only the noise figure feeds
#: the model here.  The coefficient is a placeholder pending lab calibration.
ASE_SIGMA_COEFF_DB = 0.0063


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PpeAnomaly:
    """A single anomaly in a `PpeTrace`.

    Emitters plant ground-truth anomalies with `is_root=True`.  The
    `detect_anomalies` function returns its own detector view with
    `is_root=False` — those two views are kept separate by convention so
    downstream rules / GNN can train against either or both.

    `z_km` is in **path coordinates** (cumulative distance along the OCh
    route).  `span_id` + `offset_in_span_km` are the reverse-mapped location
    (which OMS span the anomaly falls in, and how far into it) — None for
    non-spatial kinds like `rx_offset`.
    """
    kind: str                   # 'step' | 'slope' | 'null' | 'rx_offset'
    z_km: float | None          # cumulative path localisation; None if non-spatial
    z_end_km: float | None      # for 'slope', the end of the affected segment
    magnitude_db: float         # step depth, slope dB/km, or rx offset dB
    confidence: float           # in [0, 1]; emitter-planted -> 1.0
    span_id: str = ""           # reverse-mapped span the anomaly falls in
    offset_in_span_km: float | None = None   # offset within that span
    is_root: bool = False


@dataclass
class PpeTrace:
    """One end-to-end PPE snapshot for a single dropping OCh, attributed to the
    OCh's drop NE / OTU board (the RX that reconstructed it)."""
    och: str                    # OCh tunnel name, e.g. 'och_ch2'
    drop_ne: str
    drop_board: str
    total_length_km: float = 0.0
    # Cumulative start offset (km) of each span on the OCh's ERO, aligned with
    # `span_ids`.  Used for the path_z → (span, offset) reverse map.
    z_offsets: list[float] = field(default_factory=list)
    span_ids: list[str] = field(default_factory=list)
    t_offset_ms: int = 0
    t_observed_ms: int | None = None
    delta_z_m: int = DEFAULT_DELTA_Z_M
    samples: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32)
    )
    baseline: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32)
    )
    degraded: bool = False
    anomalies: list[PpeAnomaly] = field(default_factory=list)
    group_id: str = ""

    # ----- reverse map ------------------------------------------------------

    def locate(self, z_km: float) -> tuple[str, float]:
        """Map a cumulative path coordinate to ``(span_id, offset_in_span_km)``.

        Returns the span whose ``[offset, offset+length]`` interval contains
        ``z_km``; clamps to the last span for the final sample.
        """
        if not self.span_ids:
            return "", 0.0
        for i, sid in enumerate(self.span_ids):
            start = self.z_offsets[i]
            end = (self.z_offsets[i + 1] if i + 1 < len(self.z_offsets)
                   else self.total_length_km)
            if start <= z_km <= end + 1e-6:
                return sid, float(z_km - start)
        last = len(self.span_ids) - 1
        return self.span_ids[last], float(z_km - self.z_offsets[last])


# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------

def _channels_with_span(t: Topology, span_id: str) -> list[Tunnel]:
    """OCh tunnels whose primary ERO traverses `span_id`."""
    return [tn for tn in t.wdm_tunnels.values() if span_id in tn.ero]


def _channels_dropping_at(t: Topology, ne_id: str) -> list[Tunnel]:
    """OCh tunnels whose destination is `ne_id`."""
    return [tn for tn in t.wdm_tunnels.values() if tn.dst_ne == ne_id]


def _channels_sourcing_at(t: Topology, ne_id: str) -> list[Tunnel]:
    """OCh tunnels whose source is `ne_id`."""
    return [tn for tn in t.wdm_tunnels.values() if tn.src_ne == ne_id]


def _channels_touching_ne(t: Topology, ne_id: str) -> list[Tunnel]:
    """OCh tunnels with `ne_id` as source OR destination."""
    return [tn for tn in t.wdm_tunnels.values()
            if tn.src_ne == ne_id or tn.dst_ne == ne_id]


def _otu_board(t: Topology, ne_id: str) -> Board | None:
    ne = t.nes.get(ne_id)
    if ne is None:
        return None
    for b in ne.boards:
        if b.type == "OTU":
            return b
    return None


def _oa_board(t: Topology, ne_id: str) -> Board | None:
    """The NE's optical-amplifier (OA / EDFA) module, if any."""
    ne = t.nes.get(ne_id)
    if ne is None:
        return None
    for b in ne.boards:
        if b.type == "OA":
            return b
    return None


def _ppe_active(channel: Tunnel, otu: Board | None) -> bool:
    """Per-channel override overrides per-OTU enable."""
    if channel.ppe_override_enabled is not None:
        return channel.ppe_override_enabled
    return otu is not None and otu.ppe_enabled


# ---------------------------------------------------------------------------
# Amplifier model (decision D-4)
# ---------------------------------------------------------------------------

def amplifiers_for(link: Link) -> list[tuple[float, float]]:
    """Return ``[(offset_in_span_km, gain_db), …]`` for one directed span.

    Uses the explicit ``link.amplifiers`` list when present (M-2 encodes it in
    ``topology.json``); otherwise auto-derives ``span_count`` equal-spaced
    inline EDFAs (the last being the node OA at the OMS boundary, ``z = L``).
    Each EDFA's transparent gain compensates one physical span's fibre +
    nominal connector loss, minus a small under-compensation margin.
    """
    if link.amplifiers:
        return [(float(a["z_km"]), float(a["gain_db"])) for a in link.amplifiers]
    ns = link.span_count if link.span_count and link.span_count > 0 else 1
    seg = link.length_km / ns
    gain = (link.alpha_db_per_km * seg
            + DEFAULT_SPAN_CONNECTOR_DB - UNDER_COMP_MARGIN_DB)
    return [((k + 1) * seg, gain) for k in range(ns)]


# ---------------------------------------------------------------------------
# End-to-end baseline composer
# ---------------------------------------------------------------------------

def _och_geometry(t: Topology, channel: Tunnel) -> tuple[
        list[tuple[str, Link, float]], float]:
    """Return ``([(span_id, link, offset_start_km), …], total_length_km)`` for
    the OCh's ERO, skipping zero-length / unknown spans."""
    spans: list[tuple[str, Link, float]] = []
    off = 0.0
    for sid in channel.ero:
        link = t.optical_links.get(sid)
        if link is None or link.length_km <= 0:
            continue
        spans.append((sid, link, off))
        off += link.length_km
    return spans, off


def _span_offset(t: Topology, channel: Tunnel, span_id: str) -> float:
    """Cumulative path distance (km) from the TX to the start of `span_id`."""
    spans, _ = _och_geometry(t, channel)
    for sid, _link, off in spans:
        if sid == span_id:
            return off
    return 0.0


def _route_ase_amps(t: Topology, channel: Tunnel) -> list[tuple[float, float]]:
    """Return ``[(z_cumulative_km, noise_factor_linear), …]`` for each node OA
    the OCh route transits.

    One egress booster per span's source NE, positioned at that NE's
    ``edfa-z-km`` offset into the span it launches (``0`` = at NE egress).  The
    linear noise factor ``F = 10**(NF_dB/10)`` comes from the OA module's
    ``nfms:edfa-noise-figure-db`` in inventory (Design/12 §6.1).  Feeds the ASE
    term in ``_noise``; amplifier *gain* is left to the topology amplifiers.
    """
    spans, _ = _och_geometry(t, channel)
    out: list[tuple[float, float]] = []
    for _sid, link, off in spans:
        oa = _oa_board(t, link.from_ne)
        if oa is None:
            continue
        z = off + float(oa.edfa_z_km)
        f_lin = 10.0 ** (oa.edfa_noise_figure_db / 10.0)
        out.append((z, f_lin))
    return out


def baseline_och(t: Topology, channel: Tunnel) -> tuple[
        np.ndarray, np.ndarray, float, list[float], list[str], int]:
    """Compose the end-to-end noiseless baseline for one OCh.

    Returns ``(z_km, P0_dBm, total_length_km, z_offsets, span_ids, delta_z_m)``.
    ``z_km`` and ``P0_dBm`` are ``float32[N]`` over ``0 .. L_total`` (step Δz);
    empty arrays when the OCh has no usable spans.
    """
    spans, total = _och_geometry(t, channel)
    otu = _otu_board(t, channel.dst_ne)
    delta_z_m = otu.ppe_delta_z_m if otu is not None else DEFAULT_DELTA_Z_M
    z_offsets = [off for (_sid, _link, off) in spans]
    span_ids = [sid for (sid, _link, _off) in spans]
    if total <= 0.0:
        return (np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32),
                0.0, z_offsets, span_ids, delta_z_m)

    dz_km = delta_z_m / 1000.0
    n = int(round(total / dz_km)) + 1            # inclusive endpoint at z=L_total
    z = (np.arange(n, dtype=np.float64) * dz_km)
    p = np.full(n, float(channel.tx_channel_power_dbm), dtype=np.float64)

    for sid, link, off in spans:
        # Fibre attenuation: lose α·(distance travelled inside this span).
        local = np.clip(z - off, 0.0, link.length_km)
        p -= link.alpha_db_per_km * local
        # Connector / splice losses at fixed offsets within the span.
        for cl in link.connector_losses:
            zc = off + float(cl["z_km"])
            p = np.where(z >= zc, p - float(cl["loss_db"]), p)
        # EDFA gains — the sawtooth peaks (inter- + intra-OMS).
        for (azk, gain) in amplifiers_for(link):
            za = off + azk
            p = np.where(z >= za, p + gain, p)

    return (z.astype(np.float32), p.astype(np.float32),
            float(total), z_offsets, span_ids, delta_z_m)


# Back-compat thin alias used by a few callers/tests that only want the curve.
def baseline(t: Topology, channel: Tunnel) -> tuple[np.ndarray, np.ndarray]:
    """Return just ``(z_km, P0_dBm)`` for the OCh's end-to-end profile."""
    z, p0, *_ = baseline_och(t, channel)
    return z, p0


# ---------------------------------------------------------------------------
# Trace builders
# ---------------------------------------------------------------------------

def _coerce_rng(rng) -> random.Random:
    if rng is None:
        return random.Random()
    if isinstance(rng, random.Random):
        return rng
    return random.Random(rng)


def _noise(otu: Board | None, z_km: np.ndarray, rng: random.Random,
           ase_amps: list[tuple[float, float]] | None = None) -> np.ndarray:
    """Per-sample Gaussian noise with σ growing in cumulative distance.

    Beyond the OTU's base DBP σ(z), each node OA the route transits raises the
    downstream noise floor by ``ASE_SIGMA_COEFF_DB × F`` for ``z ≥ z_amp``,
    where ``F`` is that OA's linear noise factor (from
    ``nfms:edfa-noise-figure-db``).  ``ase_amps`` is the list returned by
    ``_route_ase_amps``.
    """
    sigma_0 = otu.dbp_noise_sigma_db if otu else 0.05
    sigma_per_km = otu.dbp_noise_sigma_per_km_db if otu else 0.001
    sigma = sigma_0 + sigma_per_km * z_km
    for (za, f_lin) in (ase_amps or []):
        sigma = np.where(z_km >= za, sigma + ASE_SIGMA_COEFF_DB * f_lin, sigma)
    return np.array([rng.gauss(0.0, float(s)) for s in sigma], dtype=np.float32)


def _new_trace(t: Topology, channel: Tunnel, otu: Board | None, *,
               group_id: str, rng: random.Random) -> PpeTrace:
    """Compose a clean end-to-end baseline trace with noise applied.  Emitters
    then perturb `samples` in-place to plant their δP shape."""
    z, base, total, z_offsets, span_ids, dz_m = baseline_och(t, channel)
    if len(base) == 0:
        return PpeTrace(
            och=channel.name, drop_ne=channel.dst_ne,
            drop_board=otu.uuid if otu else "",
            total_length_km=total, z_offsets=z_offsets, span_ids=span_ids,
            delta_z_m=dz_m,
            samples=np.zeros(0, dtype=np.float32),
            baseline=np.zeros(0, dtype=np.float32),
            degraded=True, group_id=group_id,
        )
    noise = _noise(otu, z, rng, _route_ase_amps(t, channel))
    samples = (base + noise).astype(np.float32)
    return PpeTrace(
        och=channel.name, drop_ne=channel.dst_ne,
        drop_board=otu.uuid if otu else "",
        total_length_km=total, z_offsets=z_offsets, span_ids=span_ids,
        delta_z_m=dz_m,
        samples=samples, baseline=base, group_id=group_id,
    )


def _degraded_trace(t: Topology, channel: Tunnel, otu: Board | None, *,
                    group_id: str) -> PpeTrace:
    """Sentinel trace for an OCh that can't be observed (dead NE / hard cut at
    the source) — `degraded=True`, `samples` all NaN, `baseline` preserved."""
    z, base, total, z_offsets, span_ids, dz_m = baseline_och(t, channel)
    n = len(base)
    return PpeTrace(
        och=channel.name, drop_ne=channel.dst_ne,
        drop_board=otu.uuid if otu else "",
        total_length_km=total, z_offsets=z_offsets, span_ids=span_ids,
        delta_z_m=dz_m,
        samples=np.full(n, np.nan, dtype=np.float32),
        baseline=base, degraded=True, group_id=group_id,
    )


def _z_axis(tr: PpeTrace) -> np.ndarray:
    n = len(tr.baseline) if len(tr.baseline) else len(tr.samples)
    return np.arange(n, dtype=np.float32) * (tr.delta_z_m / 1000.0)


# ---------------------------------------------------------------------------
# Paired fault emitters — one trace per affected dropping OCh
# ---------------------------------------------------------------------------

# --- fiber_cut --------------------------------------------------------------

def ppe_fiber_cut(t: Topology, span_id: str,
                  position_km: float | None = None,
                  rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Fiber cut at `position_km` along `span_id`.  On every OCh that traverses
    the span, the receiver's end-to-end profile shows a hard step at the
    cumulative cut position and NaN beyond it (no light gets past the cut)."""
    rng = _coerce_rng(rng)
    cut_span = t.optical_links.get(span_id)
    if cut_span is None or cut_span.length_km <= 0:
        return []
    if position_km is None:
        position_km = rng.uniform(0.1 * cut_span.length_km,
                                  0.9 * cut_span.length_km)

    out: list[PpeTrace] = []
    for ch in _channels_with_span(t, span_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        tr = _new_trace(t, ch, otu, group_id=group_id, rng=rng)
        z = _z_axis(tr)
        z_cut = _span_offset(t, ch, span_id) + float(position_km)
        tr.samples = tr.samples.copy()
        tr.samples[z >= z_cut] = np.nan
        tr.anomalies.append(PpeAnomaly(
            kind="step", z_km=z_cut, z_end_km=None,
            magnitude_db=-30.0, confidence=1.0,
            span_id=span_id, offset_in_span_km=float(position_km),
            is_root=True,
        ))
        out.append(tr)
    return out


# --- fiber_attenuation_high -------------------------------------------------

def ppe_fiber_attenuation_high(
    t: Topology, span_id: str,
    seg: tuple[float, float] | None = None,
    extra_db_per_km: float = 0.10,
    rng=None, group_id: str = "R1",
) -> list[PpeTrace]:
    """Soft attenuation over a sub-segment `[a, b]` (offsets within `span_id`):
    an extra slope on the segment, then a flat cumulative offset to the end."""
    rng = _coerce_rng(rng)
    span = t.optical_links.get(span_id)
    if span is None or span.length_km <= 0:
        return []
    L = span.length_km
    if seg is None:
        seg = (0.3 * L, 0.7 * L)
    a, b = seg

    out: list[PpeTrace] = []
    for ch in _channels_with_span(t, span_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        tr = _new_trace(t, ch, otu, group_id=group_id, rng=rng)
        z = _z_axis(tr)
        off = _span_offset(t, ch, span_id)
        z_a, z_b = off + a, off + b
        delta = np.zeros_like(z)
        in_seg = (z >= z_a) & (z <= z_b)
        delta[in_seg] = -extra_db_per_km * (z[in_seg] - z_a)
        beyond = z > z_b
        delta[beyond] = -extra_db_per_km * (b - a)
        tr.samples = (tr.samples + delta).astype(np.float32)
        tr.anomalies.append(PpeAnomaly(
            kind="slope", z_km=float(z_a), z_end_km=float(z_b),
            magnitude_db=float(extra_db_per_km), confidence=1.0,
            span_id=span_id, offset_in_span_km=float(a), is_root=True,
        ))
        out.append(tr)
    return out


# --- connector_dirty --------------------------------------------------------

def ppe_connector_dirty(t: Topology, span_id: str,
                        z_km: float, magnitude_db: float,
                        rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Dirty connector at `z_km` (offset within `span_id`), step depth
    `magnitude_db` (positive dB).  A small localised step, flat at the lowered
    level beyond — PPE catches even sub-alarm-threshold cases."""
    rng = _coerce_rng(rng)
    span = t.optical_links.get(span_id)
    if span is None or span.length_km <= 0:
        return []
    drop_db = float(magnitude_db)

    out: list[PpeTrace] = []
    for ch in _channels_with_span(t, span_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        tr = _new_trace(t, ch, otu, group_id=group_id, rng=rng)
        z = _z_axis(tr)
        z_pt = _span_offset(t, ch, span_id) + float(z_km)
        delta = np.where(z >= z_pt, -drop_db, 0.0).astype(np.float32)
        tr.samples = (tr.samples + delta).astype(np.float32)
        tr.anomalies.append(PpeAnomaly(
            kind="step", z_km=float(z_pt), z_end_km=None,
            magnitude_db=-drop_db, confidence=1.0,
            span_id=span_id, offset_in_span_km=float(z_km), is_root=True,
        ))
        out.append(tr)
    return out


# --- b1_exc_high / b2_exc_high / fec_pre_cor_high ---------------------------
# Electrical-layer faults: no PPE change.  One clean end-to-end trace per OCh
# traversing the span (a "useful negative example" — alarms but healthy PPE).

def _ppe_electrical_on_span(t: Topology, span_id: str,
                            rng, group_id: str) -> list[PpeTrace]:
    rng = _coerce_rng(rng)
    out: list[PpeTrace] = []
    for ch in _channels_with_span(t, span_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        out.append(_new_trace(t, ch, otu, group_id=group_id, rng=rng))
    return out


def ppe_b1_exc_high(t: Topology, span_id: str,
                    rng=None, group_id: str = "R1") -> list[PpeTrace]:
    return _ppe_electrical_on_span(t, span_id, rng, group_id)


def ppe_b2_exc_high(t: Topology, span_id: str,
                    rng=None, group_id: str = "R1") -> list[PpeTrace]:
    return _ppe_electrical_on_span(t, span_id, rng, group_id)


def ppe_fec_pre_cor_high(t: Topology, span_id: str,
                         rng=None, group_id: str = "R1") -> list[PpeTrace]:
    return _ppe_electrical_on_span(t, span_id, rng, group_id)


# --- rx_power_drift ---------------------------------------------------------

def ppe_rx_power_drift(t: Topology, ne_id: str, board_id: str = "",
                       port_id: str = "", direction: str = "low",
                       rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Receiver-side power drift — the optical path is unchanged; the offset of
    ±2 dB lands on the **last** sample (the receiver) of the end-to-end trace —
    the diagnostic distinguisher from a true span fault."""
    rng = _coerce_rng(rng)
    delta = -2.0 if direction == "low" else 2.0
    out: list[PpeTrace] = []
    for ch in _channels_dropping_at(t, ne_id):
        otu = _otu_board(t, ne_id)
        if not _ppe_active(ch, otu):
            continue
        tr = _new_trace(t, ch, otu, group_id=group_id, rng=rng)
        if len(tr.samples) > 0:
            tr.samples = tr.samples.copy()
            tr.samples[-1] = np.float32(tr.samples[-1] + delta)
            last_span = ch.ero[-1] if ch.ero else ""
            tr.anomalies.append(PpeAnomaly(
                kind="rx_offset", z_km=None, z_end_km=None,
                magnitude_db=float(delta), confidence=1.0,
                span_id=last_span, offset_in_span_km=None, is_root=True,
            ))
        out.append(tr)
    return out


# --- ne_power_off -----------------------------------------------------------

def ppe_ne_power_off(t: Topology, ne_id: str,
                     rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """NE goes dark: every OCh sourced at this NE delivers no light, every OCh
    dropping at this NE can't observe.  Both get a `degraded` sentinel; the
    sourced OChs carry the root `null` anomaly at z=0."""
    rng = _coerce_rng(rng)
    out: list[PpeTrace] = []
    for ch in _channels_touching_ne(t, ne_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        tr = _degraded_trace(t, ch, otu, group_id=group_id)
        if ch.src_ne == ne_id:
            tr.anomalies.append(PpeAnomaly(
                kind="null", z_km=0.0, z_end_km=None,
                magnitude_db=-99.0, confidence=1.0,
                span_id=(tr.span_ids[0] if tr.span_ids else ""),
                offset_in_span_km=0.0, is_root=True,
            ))
        out.append(tr)
    return out


# --- board_power_off --------------------------------------------------------

def ppe_board_power_off(t: Topology, ne_id: str, board_id: str,
                        rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Board powers off.  An OTU board behaves like `ne_power_off` for
    OCh-affecting purposes; a non-OTU board (OA/FIU/…) leaves the channels
    intact, so we emit clean traces for the OChs touching this NE."""
    rng = _coerce_rng(rng)
    ne = t.nes.get(ne_id)
    board = ne.board(board_id) if ne else None
    if board is None:
        return []
    if board.type == "OTU":
        return ppe_ne_power_off(t, ne_id, rng=rng, group_id=group_id)
    out: list[PpeTrace] = []
    for ch in _channels_touching_ne(t, ne_id):
        otu = _otu_board(t, ch.dst_ne)
        if not _ppe_active(ch, otu):
            continue
        out.append(_new_trace(t, ch, otu, group_id=group_id, rng=rng))
    return out


# --- fan_failure ------------------------------------------------------------

def ppe_fan_failure(t: Topology, ne_id: str,
                    rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Fan failure — thermal-induced small drift (±0.5 dB) on the receivers at
    the affected NE, surfacing on the last sample of the end-to-end trace."""
    rng = _coerce_rng(rng)
    out: list[PpeTrace] = []
    for ch in _channels_dropping_at(t, ne_id):
        otu = _otu_board(t, ne_id)
        if not _ppe_active(ch, otu):
            continue
        tr = _new_trace(t, ch, otu, group_id=group_id, rng=rng)
        if len(tr.samples) > 0:
            delta = rng.choice([-1.0, 1.0]) * 0.5
            tr.samples = tr.samples.copy()
            tr.samples[-1] = np.float32(tr.samples[-1] + delta)
            last_span = ch.ero[-1] if ch.ero else ""
            tr.anomalies.append(PpeAnomaly(
                kind="rx_offset", z_km=None, z_end_km=None,
                magnitude_db=float(delta), confidence=1.0,
                span_id=last_span, offset_in_span_km=None, is_root=True,
            ))
        out.append(tr)
    return out


# --- config_mismatch --------------------------------------------------------

def ppe_config_mismatch(t: Topology, ne_id: str, board_id: str = "",
                        port_id: str = "",
                        rng=None, group_id: str = "R1") -> list[PpeTrace]:
    """Configuration mismatch — no PPE change; clean end-to-end traces for the
    OChs dropping at this NE (negative example for the GNN)."""
    rng = _coerce_rng(rng)
    out: list[PpeTrace] = []
    for ch in _channels_dropping_at(t, ne_id):
        otu = _otu_board(t, ne_id)
        if not _ppe_active(ch, otu):
            continue
        out.append(_new_trace(t, ch, otu, group_id=group_id, rng=rng))
    return out


# ---------------------------------------------------------------------------
# Catalog + dispatcher
# ---------------------------------------------------------------------------

CATALOG = {
    "fiber_cut":              ppe_fiber_cut,
    "fiber_attenuation_high": ppe_fiber_attenuation_high,
    "connector_dirty":        ppe_connector_dirty,
    "b1_exc_high":            ppe_b1_exc_high,
    "b2_exc_high":            ppe_b2_exc_high,
    "fec_pre_cor_high":       ppe_fec_pre_cor_high,
    "rx_power_drift":         ppe_rx_power_drift,
    "ne_power_off":           ppe_ne_power_off,
    "board_power_off":        ppe_board_power_off,
    "fan_failure":            ppe_fan_failure,
    "config_mismatch":        ppe_config_mismatch,
}


def generate(t: Topology, fault_type: str, *args,
             rng=None, group_id: str = "R1", **kwargs) -> list[PpeTrace]:
    """Dispatch on fault type.  Args mirror `faults.<fault_type>` exactly."""
    if fault_type not in CATALOG:
        return []
    return CATALOG[fault_type](t, *args, rng=rng, group_id=group_id, **kwargs)


# ---------------------------------------------------------------------------
# Anomaly detector — runs on noisy `samples`, ignores planted `is_root`.
# Reverse-maps spatial detections to (span_id, offset).
# ---------------------------------------------------------------------------

def detect_anomalies(trace: PpeTrace, *,
                     step_threshold_db: float = 5.0,
                     slope_threshold_db_per_km: float = 0.05,
                     slope_min_length_km: float = 1.0) -> list[PpeAnomaly]:
    """Heuristic anomaly detector.  Returns detections with `is_root=False`,
    each carrying the reverse-mapped `(span_id, offset_in_span_km)` for spatial
    kinds."""
    out: list[PpeAnomaly] = []

    def _spatial(kind, z_km, z_end_km, mag, conf):
        span_id, offset = trace.locate(z_km)
        out.append(PpeAnomaly(
            kind=kind, z_km=float(z_km),
            z_end_km=None if z_end_km is None else float(z_end_km),
            magnitude_db=float(mag), confidence=conf,
            span_id=span_id, offset_in_span_km=offset, is_root=False,
        ))

    if trace.degraded:
        out.append(PpeAnomaly(
            kind="null", z_km=0.0, z_end_km=None,
            magnitude_db=-99.0, confidence=0.9,
            span_id=(trace.span_ids[0] if trace.span_ids else ""),
            offset_in_span_km=0.0, is_root=False,
        ))
        return out

    n = len(trace.samples)
    if n == 0:
        return out

    samples = np.asarray(trace.samples, dtype=np.float32)
    base = np.asarray(trace.baseline, dtype=np.float32)
    residual = samples - base
    dz_km = trace.delta_z_m / 1000.0
    z_km = np.arange(n, dtype=np.float32) * dz_km

    # NaN-tail detection (hard cut).
    nan_mask = np.isnan(samples)
    if nan_mask.any():
        first_nan = int(np.argmax(nan_mask))
        _spatial("step", z_km[first_nan], None, -30.0, 0.95)

    finite_residual = np.where(nan_mask, 0.0, residual)

    # Rx-offset: jump concentrated on the very last sample.  Compare the last
    # sample to a robust (median) estimate of a prior window — on long paths
    # the cumulative DBP noise is large, so use the median, not the mean.
    if n >= 4 and not nan_mask[-1] and not nan_mask[-2]:
        last = float(finite_residual[-1])
        second_last = float(finite_residual[-2])
        prior = finite_residual[max(0, n - 10):n - 2]
        prior_ref = float(np.median(prior)) if len(prior) else 0.0
        jump_last = abs(last - prior_ref)
        jump_second = abs(second_last - prior_ref)
        if jump_last > 1.0 and jump_last > 1.5 * max(jump_second, 0.3):
            out.append(PpeAnomaly(
                kind="rx_offset", z_km=None, z_end_km=None,
                magnitude_db=last - prior_ref, confidence=0.8,
                span_id=(trace.span_ids[-1] if trace.span_ids else ""),
                offset_in_span_km=None, is_root=False,
            ))

    # Step: largest residual diff above threshold (e.g. a connector loss or
    # the leading edge of a hard cut).
    if n >= 2:
        d = np.diff(finite_residual)
        idx = int(np.argmax(np.abs(d)))
        if abs(d[idx]) > step_threshold_db:
            _spatial("step", z_km[idx + 1], None, float(d[idx]), 0.85)

    # Slope: a localised attenuation ramps the residual down over a contiguous
    # segment then stays flat.  Endpoint slope over the whole route is diluted
    # on a long path, so smooth the residual (to suppress per-sample noise) and
    # look for the steepest sustained descent over a multi-km window.
    win = max(4, int(round(max(slope_min_length_km, 10.0) / dz_km)))
    if n >= win + 1:
        # Moving-average smooth over ~1 km so single-sample noise doesn't read
        # as a slope.
        k = max(1, int(round(1.0 / dz_km)))
        if k > 1:
            kernel = np.ones(k, dtype=np.float32) / k
            smooth = np.convolve(finite_residual, kernel, mode="same")
        else:
            smooth = finite_residual
        seg_slope = (smooth[win:] - smooth[:-win]) / (win * dz_km)
        j = int(np.argmin(seg_slope))                # most negative slope
        if -seg_slope[j] > slope_threshold_db_per_km:
            z0 = float(z_km[j])
            z1 = float(z_km[min(j + win, n - 1)])
            span_id, offset = trace.locate(z0)
            out.append(PpeAnomaly(
                kind="slope", z_km=z0, z_end_km=z1,
                magnitude_db=float(-seg_slope[j]), confidence=0.7,
                span_id=span_id, offset_in_span_km=offset, is_root=False,
            ))

    return out


# ---------------------------------------------------------------------------
# Polling delay — Option A: uniform jitter in [0, cadence_ms]
# ---------------------------------------------------------------------------

def apply_polling_delay(traces: list[PpeTrace], rng=None) -> list[PpeTrace]:
    """Set `t_observed_ms = t_offset_ms + Uniform(0, cadence)` per trace.
    Returns a NEW list sorted by `t_observed_ms`."""
    rng = rng or random
    out: list[PpeTrace] = []
    for tr in traces:
        cadence = 30_000
        observed = tr.t_offset_ms + rng.randint(0, cadence)
        out.append(PpeTrace(
            och=tr.och, drop_ne=tr.drop_ne, drop_board=tr.drop_board,
            total_length_km=tr.total_length_km,
            z_offsets=list(tr.z_offsets), span_ids=list(tr.span_ids),
            t_offset_ms=tr.t_offset_ms, t_observed_ms=observed,
            delta_z_m=tr.delta_z_m, samples=tr.samples, baseline=tr.baseline,
            degraded=tr.degraded, anomalies=list(tr.anomalies),
            group_id=tr.group_id,
        ))
    out.sort(key=lambda x: x.t_observed_ms or 0)
    return out
