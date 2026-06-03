# Модуль `observability`

**Статус:** **реализован (Sprint 6 — финальный)** · **Владелец кода:** `app/observability` + инфра (`infra/`)

Observability-стек проекта: **Prometheus** (метрики), **Grafana** (дашборды), **Sentry** (трейсинг исключений) — продуктовое решение [08 §6-1](../../08-product-decisions.md#sprint-6--observability-cost-scale), [ADR-015](../../adr/ADR-015-observability-stack.md). Накапливает нормативные метрики из остаточного долга S2–S5 (рекомендации финальных reviewer'ов) и закрывает наблюдаемостью [TD-010](../../100-known-tech-debt.md#td-010)/[TD-011](../../100-known-tech-debt.md#td-011)/[TD-012](../../100-known-tech-debt.md#td-012); cost-метрики обслуживают калибровку [TD-005](../../100-known-tech-debt.md#td-005)/[TD-006](../../100-known-tech-debt.md#td-006) ([Q-COST-1](../../99-open-questions.md#q-cost-1)).

## Граница
- **Не** источник истины бизнес-состояния — `Postgres`/`job_events` остаются source-of-truth. Метрики — производный наблюдаемый слой.
- **Не** влияет на пайплайн: экспозиция `/metrics` и отправка в Sentry — best-effort; их недоступность не ломает джобу/запрос.
- Инструментация живёт **рядом** с кодом модулей (api/pipeline/billing/deploy/notify), а не дублирует их логику; этот модуль фиксирует **контракт имён/типов/labels метрик** (single normative source) и конфиг-артефакты Prometheus/Grafana/Sentry.

## Документы
- [00-overview.md](00-overview.md) — scope / out-of-scope
- [03-architecture.md](03-architecture.md) — нормативная таблица метрик, Grafana-дашборды, Sentry, scrubbing, scale-наблюдаемость

## DoD (Sprint 6)
- `/metrics` (Prometheus text format) экспонируется FastAPI-app **и** Celery-воркерами (llm+build) + beat ([03-architecture §1](03-architecture.md#1-экспозиция-metrics)).
- Все метрики из нормативной таблицы ([03-architecture §2](03-architecture.md#2-нормативная-таблица-метрик)) инструментированы по месту (имя/тип/labels — символ-в-символ из таблицы).
- Sentry инициализирован для FastAPI + Celery с correlation `job_id`/`project_id`/`user_id` и scrubbing секретов ([03-architecture §4](03-architecture.md#4-sentry)).
- Grafana provisioning (datasource + dashboards as code) в `infra/grafana/` ([03-architecture §3](03-architecture.md#3-grafana-дашборды)).
- `prometheus.yml` scrape-конфиг в `infra/prometheus/` (правило конфиг-артефакта — [07-deployment.md](../../07-deployment.md#правило-конфиг-артефакта-prometheusgrafana-sprint-6)).
- Новые env (`SENTRY_*`, `METRICS_*`, `REDIS_POOL_*`, `GRAFANA_*`, `PROMETHEUS_*`) — в [07-deployment.md → env-контракт](../../07-deployment.md#канонический-список-ключей).
- Новые технологии (`prometheus-client`, `sentry-sdk`, образы `prom/prometheus`/`grafana/grafana`) — в [02-tech-stack.md](../../02-tech-stack.md).

## Статус реализации (Sprint 6 — финальный)
По факту прошедшего пайплайна (backend + devops → reviewers → qa **604 passed / coverage 83.59%** → финальный reviewer approve, `production_ready: true`) DoD модуля **реализован и покрыт тестами**: `/metrics` (app + воркеры/beat), нормативная таблица метрик инструментирована по месту, Sentry (FastAPI+Celery, scrubbing секретов), Grafana provisioning (6 дашбордов as code) + Prometheus scrape-конфиг, новые env/технологии задекларированы. Наблюдаемостью закрыты [TD-010](../../100-known-tech-debt.md#td-010)/[TD-011](../../100-known-tech-debt.md#td-011)/[TD-012](../../100-known-tech-debt.md#td-012); cost-метрики-драйверы [TD-005](../../100-known-tech-debt.md#td-005)/[TD-006](../../100-known-tech-debt.md#td-006) реализованы (тонкая калибровка по реальным данным — пост-релиз).

**Приёмочный пункт, ещё НЕ прогнанный:** живые **Prometheus scrape / Grafana render / Sentry capture на реальном DSN** (фактический сбор метрик боевым Prometheus + рендер дашбордов + захват исключения реальным Sentry-проектом) — НЕ прогонялись из-за отсутствия окружения. Метрики/Sentry/Grafana-конфиг проверены через **unit/integration с реальным Redis + моки** (604 passed, coverage 83.59%, **4 real-stack теста skip**). Реализован и покрыт тестами **код observability**; живой прогон остаётся открытым приёмочным пунктом Sprint 6 — **не выдаётся за «протестировано на живом стеке»**.

## Changelog
- 2026-06-02: создан spec Sprint 6 — Prometheus + Grafana + Sentry, нормативная таблица метрик, Redis-pool наблюдаемость, cost-калибровка ([ADR-015](../../adr/ADR-015-observability-stack.md)/[ADR-016](../../adr/ADR-016-scale-topology-redis-pool.md), [08 §6](../../08-product-decisions.md#sprint-6--observability-cost-scale)) (architect).
- 2026-06-03: **реализован (Sprint 6, финальный)** — backend+devops → reviewers → qa 604 passed / coverage 83.59% → reviewer approve, `production_ready: true`. Живой Prometheus/Grafana/Sentry на реальном DSN — открытый приёмочный пункт (4 real-stack skip) (architect — актуализация docs после реализации).
- 2026-06-03: **прод-фикс `metrics.refresh` event-loop (баг A2).** Зафиксирован нормативный паттерн выполнения async-кода из синхронной Celery-задачи ([03-architecture §7](03-architecture.md#7-async-выполнение-async-кода-из-синхронной-celery-задачи-нормативный-паттерн-loopengine)): единый `asyncio.run(<coro>)` на задачу, async-engine/asyncpg-пул создаётся и `dispose()`-ится **внутри** этого loop (не на уровне импорта модуля), запрет разделять `Future`/connection между loop'ами — устраняет интермиттентный `RuntimeError: Future attached to a different loop` (asyncpg, `app/observability/collector.py`). Применимо ко всем sync-Celery-задачам с async-DB. Реализация — backend (рефактор `collector.py` + общая обёртка)/qa (тест ≥2 прогона в одном процессе без RuntimeError) (architect).
