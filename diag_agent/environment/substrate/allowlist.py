"""Ground-truth hygiene (doc 01 §5.3): the columns the agent may read.

`fault_type`, `fault_target`, `target_kind`, `note`, `t_offset_ms`,
`group_id` and `ppe_anomalies` rows with `is_root = 1` are labels or
generator internals.  Only the scorer and the FieldOracle read them, through
`diag_agent.harness.labels.LabelReader`.
"""

ALLOWED: dict[str, frozenset[str]] = {
    "incidents": frozenset({"id", "created_at", "suppressed", "topology_id"}),
    "alarms": frozenset({"id", "incident_id", "alarm_name", "ne", "board", "port", "severity", "layer",
                         "container_id", "t_observed_ms"}),
    "ppe_traces": frozenset({"id", "incident_id", "och", "drop_ne", "drop_board", "total_length_km",
                             "t_observed_ms", "delta_z_m", "n_samples", "degraded"}),
    "ppe_anomalies": frozenset({"id", "trace_id", "kind", "z_km", "z_end_km", "span_id", "offset_in_span_km",
                                "magnitude_db", "confidence"}),
}

FORBIDDEN: dict[str, frozenset[str]] = {
    "incidents": frozenset({"fault_type", "fault_target", "target_kind", "note"}),
    "alarms": frozenset({"t_offset_ms", "group_id"}),
    "ppe_traces": frozenset({"t_offset_ms", "group_id", "samples_blob", "baseline_blob"}),
    "ppe_anomalies": frozenset({"is_root"}),
}

# The one sanctioned use of physical time: CHK_ALARM_AUDIT forces an immediate
# poll (an NMS resynchronisation).  The predicate is evaluated inside the
# substrate; t_offset_ms is never returned to the caller.
AUDIT_PREDICATE = "t_offset_ms <= ?"


class LeakageError(RuntimeError):
    pass


def check_columns(table: str, columns: list[str]) -> None:
    bad = [c for c in columns if c not in ALLOWED[table]]
    if bad:
        raise LeakageError(f"columns {bad} of {table} are not on the substrate allowlist")
