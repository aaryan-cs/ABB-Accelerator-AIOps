# S1 - PVC I/O cascade
Trigger: ./trigger.sh (touch FLUSH) OR `POST cooling-monitor:8080/flush` (L4 button) -> sustained fio (4 jobs x512m, time_based 45s, fsync=8, O_DIRECT) on the shared PVC. Intensity is Helm-tunable (FIO_JOBS/SIZE/RUNTIME/FSYNC, no rebuild). Measured: timescaledb psi_io ~0.19 @ ~88% disk util (LOG-032).
| t | expected |
|---|---|
| +0s | cooling-monitor IO storms; node disk io_time climbs |
| +10-20s | dcim_write_seconds p95 jumps (same PVC) |
| +20-45s | timescaledb WAL fsync slows; probe latency up; possible restart |
| +30-60s | ingest_queue_depth climbs; INSERT rate dips |
| +45-90s | ccr_actuation_seconds p95 breaches 100ms; interlock may trip |
Witnesses: IO PSI co-pressure (storage-domain pods), kubelet_volume_stats, blockio top (IG), no network edge needed.
Expected verdict: root=cooling-monitor; chain >=3 hops; NLP cites psi/pvc evidence.
Reset: ./reset.sh (cooldown 120s). Rehearse 20x; log pass/fail in ledger.csv.
