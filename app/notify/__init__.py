"""notify — APNs push статуса джобы на iOS (Sprint 5, ADR-013).

Best-effort нотификация (не источник истины — источник job_events/GET /jobs/{jid}).
Регистрация устройств (POST/DELETE /v1/devices) маршрутизируется api; отправка push —
Celery-задача notify.apns_push. Без credentials (.p8/APNS_*) — no-op.
"""
