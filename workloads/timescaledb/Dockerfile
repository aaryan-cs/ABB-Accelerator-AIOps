FROM timescale/timescaledb:latest-pg16
COPY init.sql /docker-entrypoint-initdb.d/90-init.sql
# Aggressive checkpointing on purpose: frequent heavy WAL flushes = honest PVC stress (MASTER_PLAN 2.2 #5)
CMD ["postgres", "-c", "max_wal_size=256MB", "-c", "checkpoint_timeout=60s", "-c", "shared_buffers=256MB"]
