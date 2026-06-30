"""Evidence acceptance gate — MASTER_PLAN section 1.4.4. The false-positive killer.

An edge enters the causal graph only if ALL THREE clauses hold:
  1. statistical: |r| >= R_PEAK at peak AND elevated at an adjacent lag window
  2. physical COUPLING: the two pods must share a real resource -- a shared
     disk/PVC (pvc) or a network dependency (ebpf). Mere PSI co-pressure (both
     stalling on the box at the same time) is NOT coupling -- it's symptom
     coincidence -- so psi alone can never form an edge; it only corroborates.
  3. temporal: src onset precedes dst onset, consistent with the lag
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lagcorr import adjacent_support

R_PEAK = 0.6
R_ADJ = 0.4
TEMPORAL_TOL_S = 10.0  # one-2 samples of slack on onset ordering
COUPLING_KINDS = ("ebpf", "pvc")  # genuine resource overlap/interdependence; "psi" only corroborates


@dataclass
class Witness:
    """Physical relationships known to the topology agent (A3)."""

    ebpf_edges: set[tuple[str, str]] = field(default_factory=set)  # directed (src, dst)
    psi_copressure: set[frozenset] = field(default_factory=set)    # {frozenset({a,b})} same node+resource
    shared_relation: set[frozenset] = field(default_factory=set)   # shared PVC / same node

    def kinds(self, src: str, dst: str) -> list[str]:
        out = []
        if (src, dst) in self.ebpf_edges or (dst, src) in self.ebpf_edges:
            out.append("ebpf")
        if frozenset((src, dst)) in self.psi_copressure:
            out.append("psi")
        if frozenset((src, dst)) in self.shared_relation:
            out.append("pvc")
        return out

    def couples(self, src: str, dst: str) -> bool:
        """True only if the pair shares a real resource (disk/PVC or a network dep).

        This is the "resources overlap/interdepend" test: edges may exist ONLY
        between such pairs. PSI co-pressure does not count -- two pods both
        stalling at once is coincidence, not interdependence.
        """
        return any(k in COUPLING_KINDS for k in self.kinds(src, dst))


def accept_edge(
    src: str,
    dst: str,
    r: float,
    lag_s: int,
    profile: dict[int, float],
    witness: Witness,
    onset_s: dict[str, float],
) -> dict | None:
    """Apply the three-clause gate. Returns edge dict with evidence list, or None."""
    # clause 1 - statistical. POSITIVE coupling only: a causal cascade/contention edge
    # means the two stalls rise TOGETHER; an anti-correlation (one stalls as the other
    # eases) is competition/coincidence, not A-causes-B, so it is not an edge.
    if r < R_PEAK or not adjacent_support(profile, lag_s, R_ADJ):
        return None
    # clause 2 - physical COUPLING: the pair must share a real resource (pvc/ebpf).
    # PSI co-pressure alone is rejected -- it only corroborates a coupled edge.
    kinds = witness.kinds(src, dst)
    if not any(k in COUPLING_KINDS for k in kinds):
        return None
    # clause 3 - temporal precedence (only checkable when both onsets exist)
    t_src, t_dst = onset_s.get(src), onset_s.get(dst)
    if t_src is not None and t_dst is not None:
        if t_src > t_dst + TEMPORAL_TOL_S:
            return None
        if lag_s > 0 and (t_dst - t_src) > 4 * lag_s + TEMPORAL_TOL_S:
            return None  # onsets too far apart to be this edge
    evidence = ["stat"] + kinds + (["temporal"] if t_src is not None and t_dst is not None else [])
    return {"src": src, "dst": dst, "r": round(float(r), 3), "lag_s": int(lag_s), "evidence": evidence}
