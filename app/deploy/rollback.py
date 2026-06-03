"""Re-deploy good-ревизии (Sprint 5, ADR-014 §B, docs/modules/deploy/03-architecture.md §7).

Передеплой ранее задеплоенной good-ревизии без новой генерации/правки. Переиспользует
deploy-lifecycle §5 (новых статусов site_deployments не вводит): новый деплой целевой
ревизии → health 200 → ТОЛЬКО тогда teardown прежнего active (active→superseded), без
downtime. current_revision_id меняется ТОЛЬКО при успехе. Субдомены не реюзаются (§2).

Источник dist (§7): готовый dist/{job_id}/dist.tgz целевой ревизии в S3 → передеплой без
сборки; отсутствует/протух → пересборка из revisions.source_artifact_ref (BUILDING-путь).

Два потребителя одной механики (ADR-014 §B/§C):
  - ручной rollback (POST .../rollback) — Celery rollback.rollback_revision;
  - авто-rollback при неудачной правке (pipeline edit-цикл) — redeploy_revision напрямую.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.ids import new_deployment_id, new_subdomain
from app.core.logging import get_logger
from app.db.models import Project, Revision, SiteDeployment
from app.db.session import session_scope
from app.deploy import docker_deploy, health, routing, sandbox, workspace
from app.deploy.traefik import live_url
from app.observability import metrics
from app.storage import s3
from app.storage.s3 import S3Storage, get_storage
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


@dataclass(frozen=True)
class RedeployResult:
    ok: bool
    detail: str
    subdomain: str | None = None
    live_url: str | None = None


async def _dist_available(storage: S3Storage, revision: Revision) -> bool:
    """True, если готовый dist/{job_id}/dist.tgz целевой ревизии доступен в S3 (§7)."""
    try:
        await storage.get_bytes(s3.dist_key(revision.created_from_job_id))
        return True
    except Exception as exc:  # noqa: BLE001 - отсутствие/ошибка S3 → путь пересборки
        logger.info(
            "rollback_dist_unavailable",
            extra={"revision_id": revision.id, "error": str(exc)},
        )
        return False


async def _materialize_dist(
    settings: Settings,
    storage: S3Storage,
    revision: Revision,
    work_dir: Path,
    site_id: str,
) -> str:
    """Готовит dist-дерево в work_dir и возвращает dist_artifact_ref.

    subdomain-режим: dist доступен → распаковка готового dist.tgz (нет npm ci/vite build,
    base дефолтный / не зависит от subdomain). Иначе — пересборка из source_artifact_ref.

    path-режим (ADR-017 §2A): cache-hit НЕДОПУСТИМ — готовый dist собран с `--base` прежнего
    (или иного) site_id, а rollback использует НОВЫЙ opaque site_id (субдомены не реюзаются,
    §2/§7). Под новым site_id ассеты прежнего base за StripPrefix 404 → ОБЯЗАТЕЛЬНА пересборка
    с `--base=/s/{новый site_id}/`. Поэтому в path-режиме всегда rebuild с актуальным base.
    """
    if not settings.routing_is_path and await _dist_available(storage, revision):
        # cache-hit: готовый dist из S3 (rollback переиспользует dist без пересборки, §2.5).
        # Только subdomain-режим: base дерева = / (от subdomain не зависит).
        metrics.dist_artifact_source_total.labels(source="cache_hit").inc()
        dist_ref = s3.dist_key(revision.created_from_job_id)
        dist_tgz = await storage.get_bytes(dist_ref)
        workspace.safe_extract_tgz(dist_tgz, work_dir)
        return dist_ref

    # Пересборка из source.tgz ревизии (та же песочница, что обычный build §7). В path-режиме
    # с `--base=/s/{site_id}/` нового site_id (CLI-флаг от воркера, не из vite.config дерева).
    metrics.dist_artifact_source_total.labels(source="rebuild").inc()
    source_tgz = await storage.get_bytes(revision.source_artifact_ref)
    build_ws = work_dir.parent / f"{work_dir.name}_build"
    try:
        manifest = workspace.read_build_manifest(source_tgz)
        workspace.safe_extract_tgz(source_tgz, build_ws)
        build_command = routing.augment_build_command(settings, manifest.command, site_id)
        result = sandbox.run_build(settings, build_ws, build_command, manifest.output_dir)
        if not result.success or result.dist_dir is None:
            raise RuntimeError("rollback rebuild failed")
        # dist готов в result.dist_dir → копируем в work_dir и публикуем в S3.
        from app.workers.tasks import _pack_dir  # анти-цикл: общий упаковщик dist

        dist_tgz = _pack_dir(result.dist_dir)
        dist_ref = await storage.put_bytes(
            s3.dist_key(revision.created_from_job_id), dist_tgz, "application/gzip"
        )
        workspace.safe_extract_tgz(dist_tgz, work_dir)
        return dist_ref
    finally:
        sandbox.cleanup_workspace(build_ws)


async def _active_deployment(session: AsyncSession, project_id: str) -> SiteDeployment | None:
    """Текущий active-деплой проекта (прежний, который вытесняется при успехе)."""
    result = await session.execute(
        select(SiteDeployment)
        .where(SiteDeployment.project_id == project_id, SiteDeployment.status == "active")
        .order_by(SiteDeployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def redeploy_revision(
    project_id: str, revision_id: str, *, kind: str = "rollback"
) -> RedeployResult:
    """Передеплой целевой good-ревизии (ADR-014 §B). Idempotent через cleanup-before-run.

    Порядок (§7): новый site_deployments (building, новый subdomain) → publish_dist +
    docker run + health. health 200 → новый active, current_revision_id←целевая, прежний
    active→superseded (teardown). health-fail → teardown нового (failed), прежний нетронут,
    current_revision_id не меняется (без downtime).

    kind (rollback/edit/generation) — label метрики lovable_redeploy_duration_seconds (§2.5).
    """
    settings = get_settings()
    storage = get_storage()
    work_dir = Path(settings.builds_root) / f"rollback_{revision_id}_dist"
    redeploy_started = time.monotonic()

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        revision = await session.get(Revision, revision_id)
        if project is None or revision is None:
            return RedeployResult(ok=False, detail="project or revision missing")

        # Новый opaque site_id (= subdomain) для этого re-deploy — субдомены НЕ реюзаются
        # (§2/§7). Генерируется ДО materialize: в path-режиме dist собирается с
        # `--base=/s/{site_id}/` именно этого нового site_id (cache-hit недопустим, см.
        # _materialize_dist). В subdomain-режиме site_id на base не влияет.
        subdomain = new_subdomain()
        url = live_url(settings, subdomain)

        try:
            dist_ref = await _materialize_dist(settings, storage, revision, work_dir, subdomain)
        except (RuntimeError, OSError, ValueError) as exc:
            sandbox.cleanup_workspace(work_dir)
            logger.warning(
                "rollback_materialize_failed",
                extra={"project_id": project_id, "revision_id": revision_id, "error": str(exc)},
            )
            return RedeployResult(ok=False, detail=f"dist preparation failed: {exc}")
        deployment = SiteDeployment(
            id=new_deployment_id(),
            project_id=project.id,
            revision_id=revision.id,
            subdomain=subdomain,
            live_url=url,
            dist_artifact_ref=dist_ref,
            build_log_ref=None,
            container_id=None,
            status="building",
        )
        session.add(deployment)
        await session.commit()

        try:
            site_dir = docker_deploy.publish_dist(settings, project.id, work_dir)
            deploy_result = docker_deploy.run_nginx_container(
                settings, project_id=project.id, subdomain=subdomain, site_dir=site_dir
            )
        except (RuntimeError, OSError) as exc:
            docker_deploy.teardown_container(f"site_{subdomain}")
            deployment.status = "failed"
            await session.commit()
            logger.warning(
                "rollback_deploy_failed",
                extra={"project_id": project_id, "subdomain": subdomain, "error": str(exc)},
            )
            return RedeployResult(ok=False, detail=f"deploy failed: {exc}")
        finally:
            sandbox.cleanup_workspace(work_dir)

        deployment.container_id = deploy_result.container_id
        await session.commit()

        health_result = await health.wait_until_live(
            settings, subdomain=subdomain, container_name=deploy_result.container_name
        )
        if not health_result.ok:
            # health-fail нового → teardown нового, прежний active НЕТРОНУТ (без downtime),
            # current_revision_id не меняется (§7 п.4).
            docker_deploy.teardown_container(deploy_result.container_name)
            deployment.status = "failed"
            await session.commit()
            logger.warning(
                "rollback_health_failed",
                extra={"project_id": project_id, "subdomain": subdomain},
            )
            return RedeployResult(ok=False, detail=f"health failed: {health_result.detail}")

        # health 200 → подтверждён новый деплой. ТОЛЬКО ТЕПЕРЬ teardown прежнего active
        # (active→superseded) + current_revision_id ← целевая ревизия (§7 п.3).
        previous = await _active_deployment(session, project.id)
        deployment.status = "active"
        project.current_revision_id = revision.id
        if previous is not None and previous.id != deployment.id:
            await asyncio.to_thread(docker_deploy.teardown_container, f"site_{previous.subdomain}")
            previous.status = "superseded"
        await session.commit()
        # Длительность re-deploy (health-200 без downtime), §2.5.
        metrics.redeploy_duration_seconds.labels(kind=kind).observe(
            time.monotonic() - redeploy_started
        )
        logger.info(
            "rollback_redeployed",
            extra={
                "project_id": project_id,
                "revision_id": revision_id,
                "subdomain": subdomain,
            },
        )
        return RedeployResult(ok=True, detail="ok", subdomain=subdomain, live_url=url)


@celery_app.task(name="deploy.rollback_revision", queue="build")
def rollback_revision(job_id: str, project_id: str, revision_id: str) -> None:
    """Celery-таска ручного rollback (POST .../rollback). queue=build (доступ к Docker).

    Re-deploy good-ревизии; прогресс наблюдаем через GET /jobs/{job_id} (re-deploy-джоба
    kind=rollback). Idempotent (acks_late): cleanup-before-run делает передеплой повторяемым.
    """
    asyncio.run(_run_rollback_job(job_id, project_id, revision_id))


async def _run_rollback_job(job_id: str, project_id: str, revision_id: str) -> None:
    """Тело rollback-джобы: BUILDING/DEPLOYING → LIVE | FAILED (re-deploy good-ревизии).

    Ленивый импорт pipeline.events — анти-цикл (rollback импортируется celery_app include).
    """
    from app.db.enums import JobState
    from app.pipeline.events import fail_job, load_job, record_event, transition

    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state in (JobState.LIVE, JobState.FAILED):
            logger.info("rollback_job_skip", extra={"job_id": job_id})
            return
        # Помечаем прогресс DEPLOYING (re-deploy), чтобы SSE/polling видели активность.
        if job.state != JobState.DEPLOYING:
            await transition(session, job, JobState.DEPLOYING, event_type="state_changed")

    result = await redeploy_revision(project_id, revision_id, kind="rollback")
    # Ручной rollback (POST .../rollback): исход по результату re-deploy (§2.5).
    metrics.rollback_total.labels(
        trigger="manual", result="success" if result.ok else "infra_error"
    ).inc()

    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None:
            return
        if result.ok:
            await transition(
                session,
                job,
                JobState.LIVE,
                event_type="state_changed",
                payload={"live_url": result.live_url, "rollback_revision_id": revision_id},
            )
        else:
            # Re-deploy не прошёл — rollback-джоба FAILED; прежняя ревизия осталась active
            # (без downtime, §7 п.4). reason=infra_error — re-deploy уже-good ревизии,
            # упавший по deploy/health, классифицируется как сбой окружения (не код сайта),
            # как и исчерпание Celery-ретраев на инфра-сбое (pipeline §C). Без расширения enum.
            await record_event(
                session, job_id, "rollback_failed", payload={"detail": result.detail}
            )
            await fail_job(session, job, failure_reason="infra_error")
