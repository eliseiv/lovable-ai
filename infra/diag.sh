#!/bin/bash
cd /opt/corelysite
echo "=== job states (read-only) ==="
docker exec corelysite-postgres-1 psql -U corelysite -d corelysite -t -c "SELECT state, count(*) FROM generation_jobs GROUP BY state;"
echo "=== active (non-paused) jobs detail ==="
docker exec corelysite-postgres-1 psql -U corelysite -d corelysite -t -c "SELECT id, state, retry_count, failure_reason, created_at FROM generation_jobs ORDER BY created_at DESC LIMIT 6;"
echo "=== worker logs (last 30) ==="
docker logs corelysite-worker-1 2>&1 | tail -30
