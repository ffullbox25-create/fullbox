#!/usr/bin/env bash
set -euo pipefail

BATCH_SIZE=500
TOTAL=0

while true; do
  DELETED="$(
    psql -d fullbox -Atq -v ON_ERROR_STOP=1 -c "
      WITH doomed AS (
        SELECT id
        FROM public.processing_app_processingprintjob
        WHERE status = 'printed'
          AND updated_at < now() - interval '7 days'
        ORDER BY updated_at, id
        LIMIT ${BATCH_SIZE}
        FOR UPDATE SKIP LOCKED
      ),
      removed AS (
        DELETE FROM public.processing_app_processingprintjob AS job
        USING doomed
        WHERE job.id = doomed.id
        RETURNING 1
      )
      SELECT count(*) FROM removed;
    "
  )"
  DELETED="${DELETED:-0}"
  TOTAL=$((TOTAL + DELETED))
  if [ "$DELETED" -lt "$BATCH_SIZE" ]; then
    break
  fi
  sleep 0.2
done

psql -d fullbox -v ON_ERROR_STOP=1 -c "VACUUM (ANALYZE) public.processing_app_processingprintjob;" >/dev/null
echo "Deleted printed jobs older than 7 days: ${TOTAL}"
