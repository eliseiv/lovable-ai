"""Celery-таски на состояние (ADR-001, docs/modules/pipeline/03-architecture.md).

queue=llm: task_interview (Agent1), task_spec (Agent2→Agent3→source.tgz в S3),
           task_fix (Agent4 Fixer, Sprint 2).
queue=build: task_build_request (vite build в песочнице), task_deploy (nginx+Traefik+health).

Каждая таска синхронна снаружи (Celery), async внутри (БД/Claude/httpx) через asyncio.run.
Переход — транзакционно (state+job_events+Redis). Crash-resumable: подхват по state.

Sprint 2 (docs §A-F): доменный build/health/validation-fail уводит джобу DEPLOYING→FIXING
(а не FAILED), Agent 4 чинит дерево, 4 гарда (§C) ограничивают цикл. Транзиентные
инфра-сбои — Celery autoretry (_RETRY_KWARGS, ADR-006), НЕ FIXING. beat-периодика
(sweeper+reconciler) — app/workers/beat_tasks.py.
"""

from __future__ import annotations

import time
from pathlib import Path

from celery import Task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.usage import count_edit_start, count_generation_start
from app.core.config import get_settings
from app.core.ids import (
    new_deployment_id,
    new_question_id,
    new_revision_id,
    new_subdomain,
)
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import (
    Answer,
    GenerationJob,
    Project,
    Question,
    Revision,
    SiteDeployment,
)
from app.db.session import session_scope
from app.deploy import docker_deploy, health, routing, sandbox, workspace
from app.deploy.traefik import live_url
from app.observability import metrics
from app.pipeline.agents.agent1 import run_agent1
from app.pipeline.agents.agent2 import run_agent2
from app.pipeline.agents.agent3 import run_agent3
from app.pipeline.agents.agent4 import run_agent4, run_agent4_editor
from app.pipeline.agents.claude_client import AgentCall
from app.pipeline.agents.structured import (
    DiagnosticsHook,
    GuardHook,
    StructuredOutputError,
    UsageHook,
)
from app.pipeline.cost import record_usage
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import fail_job, load_job, record_event, touch_heartbeat, transition
from app.pipeline.failure_signature import (
    build_failure_log,
    compute_failure_signature,
    parse_failure_log,
)
from app.pipeline.fixing import enter_fixing, latest_revision_for_job
from app.pipeline.graceful_fail import run_agent_task
from app.pipeline.guards import (
    PreCallGuardTripped,
    as_decimal,
    check_fix_guards,
    check_pre_call_guards,
)
from app.pipeline.language import detect_language, language_from_bcp47
from app.schemas.agent_output import AgentOutputError
from app.storage import s3
from app.storage.s3 import S3Storage, get_storage
from app.workers.celery_app import celery_app
from app.workers.retry_policy import MAX_RETRIES, RETRY_BACKOFF_MAX_S, TRANSIENT_EXCEPTIONS

logger = get_logger(__name__)

# Celery autoretry для тасок, делающих инфра-IO (Docker/S3/Anthropic/БД/Redis):
# ТОЛЬКО транзиентные инфра-исключения (ADR-006). Доменный build/health/validation-fail
# НЕ входит в TRANSIENT_EXCEPTIONS — он уводит джобу в FIXING явным переходом state-machine,
# а не task.retry(), иначе acks_late-повтор детерминированно-падающего кода сожжёт
# max_retries и не посчитает retry_count/no-progress.
_RETRY_KWARGS: dict[str, object] = {
    "autoretry_for": TRANSIENT_EXCEPTIONS,
    "retry_backoff": True,
    "retry_backoff_max": RETRY_BACKOFF_MAX_S,
    "retry_jitter": True,
    "max_retries": MAX_RETRIES,
}


# --- Structured-output хуки агентов (ADR-020 §I): guard перед вызовом / usage после / диаг ---


def _make_agent_hooks(
    session: AsyncSession, job: GenerationJob, agent: str
) -> tuple[GuardHook, UsageHook, DiagnosticsHook]:
    """Строит before_call/after_call/on_attempt_failure для structured-агента (ADR-020 §I).

    before_call: budget/wall-clock-гард §C(b)/(c) ПЕРЕД КАЖДЫМ LLM-вызовом (включая retry —
    §I.3: ретраи не обходят бюджет). Бросок PreCallGuardTripped прерывает шаг → task
    терминализует FAILED(reason). after_call: запись llm_usage + spend ПОСЛЕ КАЖДОГО вызова
    (включая retry) + commit, чтобы следующий before_call увидел накопленный spend (Postgres —
    источник истины бюджета §C(b)). on_attempt_failure: диагностика parse/schema-фейла (§I.4) —
    лог + job_events.payload (имя агента/attempt/класс/текст ошибки/scrubbed усечённый raw).
    """

    async def before_call() -> None:
        check_pre_call_guards(job)

    async def after_call(call: AgentCall) -> None:
        await record_usage(session, job, agent, call)
        # Коммит, чтобы накопленный spend дошёл до budget-гарда следующего retry-вызова §I.3.
        await session.commit()

    async def on_attempt_failure(
        *,
        agent: str,
        attempt: int,
        max_attempts: int,
        error_text: str,
        fail_class: str,
        raw_tail: str,
    ) -> None:
        logger.warning(
            "agent_output_invalid_attempt",
            extra={
                "job_id": job.id,
                "agent": agent,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "fail_class": fail_class,
                "error": error_text,
            },
        )
        await record_event(
            session,
            job.id,
            "agent_output_invalid",
            payload={
                "agent": agent,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "fail_class": fail_class,
                "error": error_text,
                "raw_tail": raw_tail,
            },
        )
        await session.commit()

    return before_call, after_call, on_attempt_failure


# --- task_interview (CREATED → INTERVIEWING → AWAITING_CLARIFICATION) ---


async def _interview(job_id: str) -> None:
    settings = get_settings()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.CREATED:
            logger.info("interview_skip", extra={"job_id": job_id})
            return
        # Guard soft-delete (ADR-011 §C): не стартуем Agent 1 на удаляемый проект (жжёт токены).
        if await _abort_if_project_deleted(session, job):
            return
        # Sprint 3.5: usage_counters инкрементится на УСПЕШНОМ старте генерации
        # (CREATED→INTERVIEWING, фактический старт Agent 1), не на POST /projects / /answers
        # (docs/modules/billing/03 §5). Идемпотентно по job_id (guard от acks_late/реплея) —
        # коммитится одной транзакцией с переходом ниже (transition делает commit).
        await count_generation_start(session, job)
        # project гарантированно жив и не None — проверено _abort_if_project_deleted выше.
        project = await session.get(Project, job.project_id)
        assert project is not None
        # ADR-028: детерминированный серверный детект языка из ИСХОДНОГО project.prompt
        # (script-эвристика) ОДИН РАЗ, ДО Agent 1. Результат — crash-устойчивый якорь в
        # generation_jobs.content_language, фиксируется в той же транзакции, что и transition
        # в INTERVIEWING (первый commit). _interview стартует только из state==CREATED, поэтому
        # детект выполняется ровно один раз за джобу (передетекта при crash-resume нет — на
        # фазе spec язык читается из БД, см. _spec).
        language = detect_language(project.prompt)
        job.content_language = language.bcp47
        await transition(
            session,
            job,
            JobState.INTERVIEWING,
            event_type="agent_started",
            payload={"agent": "agent1", "content_language": language.bcp47},
        )
        # ADR-020 §I: usage пишется хуком after_call ПОСЛЕ каждого LLM-вызова (включая retry).
        before_call, after_call, on_fail = _make_agent_hooks(session, job, "agent1")
        try:
            result = await run_agent1(
                settings,
                project.prompt,
                language,
                before_call=before_call,
                after_call=after_call,
                on_attempt_failure=on_fail,
            )
        except PreCallGuardTripped as exc:
            # Budget/wall-clock §C(b)/(c) исчерпан перед/между retry-вызовами (ADR-020 §I.3).
            await fail_job(session, job, failure_reason=exc.reason)
            logger.warning("agent1_guard", extra={"job_id": job_id, "reason": exc.reason})
            return
        except (StructuredOutputError, ValueError, AgentOutputError) as exc:
            # Ретраи structured-output исчерпаны → FAILED(invalid_agent_output) (§I.3, Agent 1).
            await fail_job(
                session,
                job,
                failure_reason="invalid_agent_output",
                last_failure_signature="agent1_output_invalid",
            )
            logger.warning("agent1_failed", extra={"job_id": job_id, "error": str(exc)})
            return

        for q in result.questions:
            session.add(
                Question(
                    id=new_question_id(),
                    job_id=job.id,
                    position=q.position,
                    text=q.text,
                    kind=q.kind,
                    options=q.options,
                )
            )
        await record_event(
            session,
            job.id,
            "question_posted",
            payload={"count": len(result.questions)},
        )
        await transition(
            session,
            job,
            JobState.AWAITING_CLARIFICATION,
            event_type="state_changed",
            payload={"reason": "questions_ready"},
        )
        # Пауза human-in-the-loop: задач в очереди нет (резюм из POST /answers).


@celery_app.task(name="pipeline.task_interview", queue="llm", bind=True, **_RETRY_KWARGS)
def task_interview(self: Task, job_id: str) -> None:
    # ADR-019 §G: graceful-fail при недоступности LLM (исчерпание ретраев на 429/5xx/timeout
    # ИЛИ немедленно на 401/403/400) → FAILED(agent_unavailable), джоба не висит в активном
    # state, слот освобождается. Транзиентные сбои до исчерпания — обычный Celery autoretry.
    # requires_llm=True: per-job fail-fast preflight пустого ANTHROPIC_API_KEY (§Fix round 3 п.1).
    run_agent_task(self, lambda: _interview(job_id), job_id, requires_llm=True)


# --- task_spec (SPECCING → BUILDING): Agent2 → Agent3 → source.tgz в S3 ---


async def _spec(job_id: str) -> None:
    settings = get_settings()
    storage = get_storage()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.SPECCING:
            logger.info("spec_skip", extra={"job_id": job_id})
            return
        # Guard soft-delete (ADR-011 §C): не зовём Agent 2/3 на удаляемый проект.
        if await _abort_if_project_deleted(session, job):
            return
        # project гарантированно жив — проверено выше.
        project = await session.get(Project, job.project_id)
        assert project is not None

        qa_pairs = await _load_qa_pairs(session, job_id)
        await record_event(session, job.id, "agent_started", payload={"agent": "agent2"})
        await session.commit()

        # ADR-028 crash-resume: язык НЕ передетектится на фазе spec — берётся из зафиксированного
        # на фазе interview generation_jobs.content_language (единый серверный детект из исходного
        # промпта, переживший рестарт воркера между фазами). Инжектируется в директиву Agent 2.
        language = language_from_bcp47(job.content_language)

        # Agent 2: спека. usage пишется хуком after_call (ADR-020 §I) — без отдельного record.
        a2_before, a2_after, a2_fail = _make_agent_hooks(session, job, "agent2")
        try:
            spec_result = await run_agent2(
                settings,
                project.prompt,
                qa_pairs,
                language,
                before_call=a2_before,
                after_call=a2_after,
                on_attempt_failure=a2_fail,
            )
        except PreCallGuardTripped as exc:
            await fail_job(session, job, failure_reason=exc.reason)
            logger.warning("agent2_guard", extra={"job_id": job_id, "reason": exc.reason})
            return
        except (StructuredOutputError, ValueError, AgentOutputError) as exc:
            await fail_job(
                session,
                job,
                failure_reason="invalid_agent_output",
                last_failure_signature="agent2_output_invalid",
            )
            logger.warning("agent2_failed", extra={"job_id": job_id, "error": str(exc)})
            return

        spec_md = spec_result.spec_markdown
        if len(spec_md.encode("utf-8")) <= settings.spec_inline_max_bytes:
            job.spec_tz = spec_md
            job.spec_ref = None
        else:
            ref = await storage.put_text(s3.spec_key(job_id), spec_md, "text/markdown")
            job.spec_tz = None
            job.spec_ref = ref
        await session.commit()

        # Agent 3: дерево файлов (валидируется строго).
        await record_event(session, job.id, "agent_started", payload={"agent": "agent3"})
        await session.commit()
        # usage пишется хуком after_call ПОСЛЕ каждого вызова (включая retry), ADR-020 §I.3 —
        # даже когда финальный output невалиден (вызовы оплачены). Доменная валидация дерева —
        # поверх tool-use (§I.1): на исчерпании ретраев → AgentOutputError.
        a3_before, a3_after, a3_fail = _make_agent_hooks(session, job, "agent3")
        try:
            build_result = await run_agent3(
                settings,
                spec_md,
                before_call=a3_before,
                after_call=a3_after,
                on_attempt_failure=a3_fail,
            )
        except PreCallGuardTripped as exc:
            await fail_job(session, job, failure_reason=exc.reason)
            logger.warning("agent3_guard", extra={"job_id": job_id, "reason": exc.reason})
            return
        except AgentOutputError as exc:
            await fail_job(
                session,
                job,
                failure_reason="invalid_agent_output",
                last_failure_signature=exc.signature,
            )
            logger.warning("agent3_invalid", extra={"job_id": job_id, "error": str(exc)})
            return
        except (StructuredOutputError, ValueError) as exc:
            await fail_job(
                session,
                job,
                failure_reason="invalid_agent_output",
                last_failure_signature="agent3_output_invalid",
            )
            logger.warning("agent3_failed", extra={"job_id": job_id, "error": str(exc)})
            return

        # Упаковка source.tgz → S3.
        source_tgz = workspace.pack_source_tgz(build_result.tree)
        source_ref = await storage.put_bytes(s3.source_key(job_id), source_tgz, "application/gzip")
        await record_event(session, job.id, "source_packed", payload={"source_ref": source_ref})
        await transition(
            session,
            job,
            JobState.BUILDING,
            event_type="state_changed",
            payload={"source_ref": source_ref},
        )
        dispatch_for_state(job.id, JobState.BUILDING)


@celery_app.task(name="pipeline.task_spec", queue="llm", bind=True, **_RETRY_KWARGS)
def task_spec(self: Task, job_id: str) -> None:
    # ADR-019 §G: graceful-fail при недоступности LLM (Agent 2/3) → FAILED(agent_unavailable).
    # requires_llm=True: per-job fail-fast preflight пустого ANTHROPIC_API_KEY (§Fix round 3 п.1).
    run_agent_task(self, lambda: _spec(job_id), job_id, requires_llm=True)


# --- task_build_request (BUILDING → DEPLOYING): vite build в песочнице ---


async def _build_request(job_id: str) -> None:
    settings = get_settings()
    storage = get_storage()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.BUILDING:
            logger.info("build_skip", extra={"job_id": job_id})
            return
        # Guard soft-delete (ADR-011 §C): не запускаем песочницу-сборку на удаляемый проект.
        if await _abort_if_project_deleted(session, job):
            return

        # Path-режим (ADR-017 §2A): site_id обязан быть известен ДО build, чтобы собрать с
        # `vite --base=/s/{site_id}/` (иначе ассеты за StripPrefix 404). Стабилен на deploy и
        # в fix-loop rebuild (один job_id → один site_id, персист в job_events). Subdomain-
        # режим: base дефолтный /, site_id генерируется на deploy (поведение S1-S6).
        site_id: str | None = None
        if settings.routing_is_path:
            site_id = await _resolve_site_id(session, job_id)
            await session.commit()

        await record_event(session, job.id, "build_started")
        await session.commit()

        source_tgz = await storage.get_bytes(s3.source_key(job_id))
        ws = Path(settings.builds_root) / job_id
        dist_tgz: bytes | None = None
        build_log = ""
        exit_code: int | None = None
        # Sprint 6 (ADR-015 §2.1): длительность npm ci && vite build по исходу (success/fail).
        build_started = time.monotonic()
        try:
            # Валидированные build.command/output_dir провезены в source.tgz манифестом
            # (.build.json) — читаем их, чтобы недефолтные значения дошли до песочницы.
            manifest = workspace.read_build_manifest(source_tgz)
            workspace.safe_extract_tgz(source_tgz, ws)
            # Path-режим: воркер инжектит `--base=/s/{site_id}/` как CLI-флаг (НЕ из vite.config
            # LLM-дерева — безопасность, 05-security threat-model). Subdomain: команда без base.
            build_command = (
                routing.augment_build_command(settings, manifest.command, site_id)
                if site_id is not None
                else manifest.command
            )
            result = sandbox.run_build(settings, ws, build_command, manifest.output_dir)
            build_log = result.log
            if result.success and result.dist_dir is not None:
                dist_tgz = _pack_dir(result.dist_dir)
            else:
                exit_code = 1
        except (ValueError, OSError) as exc:
            build_log += f"\n[build error: {exc}]"
            exit_code = 1
        finally:
            sandbox.cleanup_workspace(ws)
            metrics.build_duration_seconds.labels(
                result="success" if dist_tgz is not None else "fail"
            ).observe(time.monotonic() - build_started)

        if dist_tgz is None:
            # Доменный build-fail (Sprint 2, docs §B): не FAILED, а DEPLOYING→FIXING.
            # failure_log пишется в S3 enter_fixing'ом с машинной шапкой (§F);
            # failure_signature считается на входе в FIXING (task_fix), не здесь.
            revision = await latest_revision_for_job(session, job_id)
            await enter_fixing(
                session,
                job,
                storage,
                failure_class=_classify_build_failure(build_log),
                failure_body=build_log,
                revision_no=revision.revision_no if revision is not None else None,
                exit_code=exit_code,
            )
            return

        dist_ref = await storage.put_bytes(s3.dist_key(job_id), dist_tgz, "application/gzip")
        log_ref = await storage.put_text(
            s3.build_log_key(job_id, job.retry_count), build_log, "text/plain"
        )
        await record_event(
            session,
            job.id,
            "build_succeeded",
            payload={"dist_ref": dist_ref, "build_log_ref": log_ref},
        )
        await transition(
            session,
            job,
            JobState.DEPLOYING,
            event_type="state_changed",
            payload={"dist_ref": dist_ref, "build_log_ref": log_ref},
        )
        dispatch_for_state(job.id, JobState.DEPLOYING)


@celery_app.task(name="pipeline.task_build_request", queue="build", bind=True, **_RETRY_KWARGS)
def task_build_request(self: Task, job_id: str) -> None:
    # ADR-019 §G/§D: исчерпание ретраев на не-LLM инфра-сбое (Docker/S3/БД) → FAILED(infra_error)
    # (не «путь в никуда»). LLM-сбоев тут нет (сборка, не Claude) — reason всегда infra_error.
    run_agent_task(self, lambda: _build_request(job_id), job_id)


# --- task_deploy (DEPLOYING → LIVE): nginx + Traefik + health-check ---


async def _deploy(job_id: str) -> None:
    settings = get_settings()
    storage = get_storage()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.DEPLOYING:
            logger.info("deploy_skip", extra={"job_id": job_id})
            return
        # Гонка GC↔in-flight виток (ADR-011 §C): проект soft-deleted/удалён между
        # постановкой task_deploy и его исполнением → FAILED(project_deleted|project_missing);
        # частичные ресурсы (если успели) снесёт project.gc.
        if await _abort_if_project_deleted(session, job):
            return
        # project гарантированно жив — проверено выше.
        project = await session.get(Project, job.project_id)
        assert project is not None

        await record_event(session, job.id, "deploy_started")
        await session.commit()

        dist_tgz = await storage.get_bytes(s3.dist_key(job_id))
        site_ws = Path(settings.builds_root) / f"{job_id}_dist"
        workspace.safe_extract_tgz(dist_tgz, site_ws)

        # Ревизия-кандидат текущей джобы. В fix-loop её создаёт task_fix (Agent 4 патч);
        # на первом деплое (happy-path S1) ревизии ещё нет — создаём здесь. Так в одном
        # витке нет дубля ревизий, а S1-поведение сохранено.
        revision = await latest_revision_for_job(session, job_id)
        if revision is None:
            revision = await _create_revision(session, project, job_id, s3.source_key(job_id))
            await session.commit()

        # site_id (= subdomain) по режиму: path — стабильный, назначенный на фазе build
        # (тот же, с которым собран `--base`, иначе ассеты за StripPrefix 404); subdomain —
        # свежий opaque на деплое (поведение S1-S6). Единый источник live_url — routing-режим.
        if settings.routing_is_path:
            subdomain = await _resolve_site_id(session, job_id)
            await session.commit()
        else:
            subdomain = new_subdomain()
        container_name = f"site_{subdomain}"
        url = live_url(settings, subdomain)

        # Строка деплоя создаётся в статусе building ДО docker run + health-check:
        # lifecycle docs §5 (building → active | failed). Фактический container_id
        # дописываем после успешного run.
        deployment = SiteDeployment(
            id=new_deployment_id(),
            project_id=project.id,
            revision_id=revision.id,
            subdomain=subdomain,
            live_url=url,
            dist_artifact_ref=s3.dist_key(job_id),
            build_log_ref=s3.build_log_key(job_id, job.retry_count),
            container_id=None,
            status="building",
        )
        session.add(deployment)
        await session.commit()

        try:
            site_dir = docker_deploy.publish_dist(settings, project.id, site_ws)
            deploy_result = docker_deploy.run_nginx_container(
                settings, project_id=project.id, subdomain=subdomain, site_dir=site_dir
            )
        except (RuntimeError, OSError) as exc:
            # teardown-on-fail (docs §5 «Инвариант фейла»): снести уже/частично
            # запущенный контейнер этой попытки + освободить subdomain, выставить
            # status=failed ДО перевода джобы в FIXING. Идемпотентно.
            docker_deploy.teardown_container(container_name)
            deployment.status = "failed"
            # Доменный deploy-fail (docs §B): DEPLOYING→FIXING (не FAILED). Класс —
            # deploy_error (старт/публикация контейнера), НЕ health_timeout: иначе
            # container-start-fail и реальный health-таймаут слились бы в один класс
            # и сигнатуру (искажает диагностику Agent 4 и no-progress §C(d)).
            await enter_fixing(
                session,
                job,
                storage,
                failure_class="deploy_error",
                failure_body=f"nginx container failed to start: {exc}",
                revision_no=revision.revision_no,
            )
            logger.warning("deploy_failed", extra={"job_id": job_id, "error": str(exc)})
            return
        finally:
            sandbox.cleanup_workspace(site_ws)

        deployment.container_id = deploy_result.container_id
        await session.commit()

        health_result = await health.wait_until_live(
            settings, subdomain=subdomain, container_name=deploy_result.container_name
        )
        if not health_result.ok:
            # teardown-on-fail (docs §5): health timeout/!=200 → docker rm -f контейнера
            # текущей попытки (снимает --restart unless-stopped, Traefik-route уберётся
            # через Docker-лейблы) + освобождение subdomain → status=failed ДО FIXING.
            docker_deploy.teardown_container(deploy_result.container_name)
            deployment.status = "failed"
            # Доменный health-fail (docs §B): DEPLOYING→FIXING (не FAILED).
            await enter_fixing(
                session,
                job,
                storage,
                failure_class=_classify_health_failure(health_result.detail),
                failure_body=f"health check failed: {health_result.detail}",
                revision_no=revision.revision_no,
            )
            logger.warning(
                "health_failed", extra={"job_id": job_id, "detail": health_result.detail}
            )
            return

        # TOCTOU-перепроверка soft-delete (ADR-011 §C / deploy §6): первый guard был ДО
        # docker run, но run_nginx_container + длинный wait_until_live могли разойтись с
        # project.gc шаг 2 (teardown читает subdomain'ы и сносит контейнеры). Если GC
        # отработал teardown ДО фактического docker run этой попытки — он не нашёл контейнера
        # (no-op), а мы затем создали orphan nginx (--restart unless-stopped) + Traefik-route,
        # переживающий GC (subdomain-takeover). Перечитываем deleted_at из БД в этой
        # транзакции (GC коммитит в отдельной сессии); при удалении — teardown текущего
        # контейнера + status=failed + FAILED(project_deleted) вместо LIVE (тот же
        # teardown-инвариант, что и на deploy/health-fail). Частичный остаток снесёт GC.
        await session.refresh(project, attribute_names=["deleted_at"])
        if project.deleted_at is not None:
            docker_deploy.teardown_container(deploy_result.container_name)
            deployment.status = "failed"
            await fail_job(session, job, failure_reason="project_deleted")
            logger.info(
                "deploy_abort_deleted_post_run",
                extra={"job_id": job_id, "subdomain": subdomain},
            )
            return

        # ADR-029 §B — re-read state-guard перед записью LIVE. docker run + wait_until_live
        # длятся минуты; in-memory job.state остался DEPLOYING с момента загрузки, но reconciler
        # в ОТДЕЛЬНОЙ сессии мог за это время записать FAILED(stuck_timeout/wall_clock_exceeded)
        # (ADR-019 §E2). Перечитываем job.state из БД (по аналогии с project.deleted_at-перечиткой
        # выше) и пишем LIVE ТОЛЬКО если джоба ещё DEPLOYING. Иначе — джоба уже терминализирована:
        # снимаем deploy-контейнер тем же teardown-инвариантом (как project_deleted-ветка) и НЕ
        # пишем LIVE. B — оптимизация (таска раньше узнаёт, что результат не нужен, не плодит
        # orphan-эффект); корректность держит CAS-барьер A в transition() даже без этого guard'а.
        await session.refresh(job, attribute_names=["state"])
        if job.state != JobState.DEPLOYING:
            docker_deploy.teardown_container(deploy_result.container_name)
            deployment.status = "failed"
            await session.commit()
            logger.info(
                "deploy_skip_terminalized",
                extra={"job_id": job_id, "state": job.state.value, "subdomain": subdomain},
            )
            return

        deployment.status = "active"

        project.current_revision_id = revision.id
        await record_event(
            session,
            job.id,
            "deployed",
            payload={"live_url": url, "subdomain": subdomain},
        )
        await transition(
            session,
            job,
            JobState.LIVE,
            event_type="state_changed",
            payload={"live_url": url},
        )


@celery_app.task(name="pipeline.task_deploy", queue="build", bind=True, **_RETRY_KWARGS)
def task_deploy(self: Task, job_id: str) -> None:
    # ADR-019 §G/§D: исчерпание ретраев на не-LLM инфра-сбое (Docker/health-транспорт/S3/БД)
    # → FAILED(infra_error). Доменный deploy/health-fail в _deploy уже уводит в FIXING.
    run_agent_task(self, lambda: _deploy(job_id), job_id)


# --- task_fix (FIXING → BUILDING | FAILED): Agent 4 Fixer + 4 гарда (docs §A-C) ---


async def _fix(job_id: str) -> None:
    settings = get_settings()
    storage = get_storage()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.FIXING:
            logger.info("fix_skip", extra={"job_id": job_id})
            return
        # Guard soft-delete (ADR-011 §C): не зовём Agent 4 Fixer на удаляемый проект.
        if await _abort_if_project_deleted(session, job):
            return
        if job.failure_log_ref is None:
            # Инвариант входа в FIXING (docs §B): failure_log_ref обязан быть записан.
            await fail_job(session, job, failure_reason="infra_error")
            logger.warning("fix_no_failure_log", extra={"job_id": job_id})
            return

        # 1. Загрузить failure_log последнего фейла, распарсить шапку (§F), посчитать
        #    failure_signature (ADR-005) ДО вызова Agent 4 — для гарда no-progress.
        failure_log = (await storage.get_bytes(job.failure_log_ref)).decode(
            "utf-8", errors="replace"
        )
        parsed = parse_failure_log(failure_log)
        signature = compute_failure_signature(failure_log)

        # Sprint 6 (TD-006, observability §5.2): read-through Redis budget кэш-гейт ПЕРЕД
        # дорогим Agent-4-вызовом. Cache-hit budget:{job_id} >= budget_usd → исчерпан без
        # Postgres; cache-miss → fallback на spend_usd (Postgres source-of-truth) + пере-засев.
        # Гард (b) ниже остаётся авторитетным (Postgres) — кэш лишь ускоряет.
        from app.observability.budget_cache import budget_exhausted

        if await budget_exhausted(job.id, as_decimal(job.spend_usd), as_decimal(job.budget_usd)):
            if not await _finalize_fix_failure(
                session, job, failure_reason="budget_exhausted", signature=signature
            ):
                logger.info("fix_budget_cache_gate", extra={"job_id": job_id})
            return

        # 2. Четыре гарда §C на входе в FIXING (до Agent 4). Гард no-progress (d) —
        #    единственная точка записи last_failure_signature. Исчерпание → FAILED(reason).
        guard = check_fix_guards(job, failure_signature=signature)
        if not guard.ok:
            # Edit-джоба (ADR-014 §C): исчерпание гарда → авто-rollback на прежнюю
            # good-ревизию, FAILED(edit_failed_rolled_back), сайт остаётся LIVE. Generation —
            # штатный FAILED(reason гарда).
            if not await _finalize_fix_failure(
                session,
                job,
                failure_reason=guard.reason or "build_unrecoverable",
                signature=signature,
            ):
                logger.info("fix_guard_tripped", extra={"job_id": job_id, "reason": guard.reason})
            return
        # Записываем перезаписанную сигнатуру (гард d) — коммит общей транзакцией ниже.
        await session.commit()

        # 3. Подготовить вход Agent 4: спека + дерево последней ревизии текущей джобы.
        spec_md = await _load_spec(session, storage, job)
        revision = await latest_revision_for_job(session, job_id)
        if revision is None:
            # Нет ревизии-кандидата (первый build-fail до создания ревизии): берём
            # source.tgz по детерминированному ключу джобы.
            source_tgz = await storage.get_bytes(s3.source_key(job_id))
        else:
            source_tgz = await storage.get_bytes(revision.source_artifact_ref)

        await record_event(session, job.id, "agent_started", payload={"agent": "agent4"})
        await session.commit()

        # 4. Вызвать Agent 4. usage пишется хуком after_call ПОСЛЕ каждого вызова (включая
        #    retry, ADR-020 §I.3). Невалидный патч после ретраев = fix-неудача (AgentOutputError)
        #    — повторный вход в FIXING (учёт в retry_count/no-progress, §A). budget/wall-clock
        #    проверяются перед КАЖДЫМ retry-вызовом (§I.3): исчерпание → штатный FAILED(reason).
        a4_before, a4_after, a4_fail = _make_agent_hooks(session, job, "agent4")
        try:
            result = await run_agent4(
                settings,
                spec_markdown=spec_md,
                source_tgz=source_tgz,
                failure_class=parsed.failure_class,
                failure_log=failure_log,
                before_call=a4_before,
                after_call=a4_after,
                on_attempt_failure=a4_fail,
            )
        except PreCallGuardTripped as exc:
            await _finalize_fix_failure(
                session, job, failure_reason=exc.reason, signature=signature
            )
            logger.warning("agent4_guard", extra={"job_id": job_id, "reason": exc.reason})
            return
        except (AgentOutputError, StructuredOutputError) as exc:
            await _handle_invalid_patch(session, storage, job, exc, revision)
            logger.warning("agent4_invalid", extra={"job_id": job_id, "error": str(exc)})
            return

        # 5a. Сигнал unrecoverable → для generation FIXING→FAILED(fixer_gave_up); для edit —
        #     авто-rollback на прежнюю good-ревизию, FAILED(edit_failed_rolled_back) (ADR-014 §C).
        if result.unrecoverable is not None:
            await record_event(
                session,
                job.id,
                "fixer_gave_up",
                payload={
                    "reason": result.unrecoverable.reason,
                    "explanation": result.unrecoverable.explanation,
                },
            )
            await session.commit()
            await _finalize_fix_failure(
                session, job, failure_reason="fixer_gave_up", signature=signature
            )
            return

        # 5b. Валидный патч → новый source.tgz (перезапись детерминированного ключа),
        #     новая ревизия той же джобы, retry_count++ (docs §B п.3), FIXING → BUILDING
        #     (диспетчер ставит task_build_request, queue=build; передеплой идемпотентен
        #     через cleanup-before-run). last_failure_signature здесь НЕ трогается (§B п.3).
        assert result.tree is not None
        project = await session.get(Project, job.project_id)
        if project is None:
            await fail_job(session, job, failure_reason="project_missing")
            return
        source_tgz_new = workspace.pack_source_tgz(result.tree)
        source_ref = await storage.put_bytes(
            s3.source_key(job_id), source_tgz_new, "application/gzip"
        )
        await _create_revision(session, project, job_id, source_ref)
        job.retry_count += 1
        await record_event(
            session,
            job.id,
            "fix_applied",
            payload={"source_ref": source_ref, "retry_count": job.retry_count},
        )
        await transition(
            session,
            job,
            JobState.BUILDING,
            event_type="state_changed",
            payload={"source_ref": source_ref, "retry_count": job.retry_count},
        )
        dispatch_for_state(job.id, JobState.BUILDING)


@celery_app.task(name="pipeline.task_fix", queue="llm", bind=True, **_RETRY_KWARGS)
def task_fix(self: Task, job_id: str) -> None:
    # ADR-019 §G: graceful-fail при недоступности LLM (Agent 4) → FAILED(agent_unavailable).
    # requires_llm=True: per-job fail-fast preflight пустого ANTHROPIC_API_KEY (§Fix round 3 п.1).
    run_agent_task(self, lambda: _fix(job_id), job_id, requires_llm=True)


# --- task_edit (CREATED → BUILDING): Agent 4 editor (Sprint 5, ADR-014 §A) ---


async def _edit(job_id: str) -> None:
    """Старт edit-джобы: Agent 4 editor (спека + current good-ревизия + instruction) →
    новое дерево → новая ревизия → BUILDING (далее штатный build/deploy → LIVE).

    edit_usage_counters инкрементится на УСПЕШНОМ старте (постановка первой обработки),
    идемпотентно по job_id (ADR-014 §A / billing §7). Невалидный output editor /
    unrecoverable → авто-rollback на прежнюю good-ревизию (FAILED(edit_failed_rolled_back)).
    """
    settings = get_settings()
    storage = get_storage()
    async with session_scope() as session:
        job = await load_job(session, job_id)
        if job is None or job.state != JobState.CREATED or job.kind != "edit":
            logger.info("edit_skip", extra={"job_id": job_id})
            return
        if await _abort_if_project_deleted(session, job):
            return
        project = await session.get(Project, job.project_id)
        assert project is not None

        instruction, base_revision = await _load_edit_request(session, job_id, project)
        if instruction is None or base_revision is None:
            # Базовая good-ревизия исчезла / нет инструкции — нечего править.
            await fail_job(session, job, failure_reason="invalid_agent_output")
            logger.warning("edit_no_base", extra={"job_id": job_id})
            return

        # Инкремент edit_usage на успешном старте edit-джобы (идемпотентно по job_id).
        await count_edit_start(session, job)
        await record_event(session, job.id, "agent_started", payload={"agent": "agent4_editor"})
        await session.commit()

        spec_md = await _load_spec(session, storage, job)
        source_tgz = await storage.get_bytes(base_revision.source_artifact_ref)

        # usage пишется хуком after_call ПОСЛЕ каждого вызова (включая retry), ADR-020 §I.3.
        ed_before, ed_after, ed_fail = _make_agent_hooks(session, job, "agent4")
        try:
            result = await run_agent4_editor(
                settings,
                spec_markdown=spec_md,
                source_tgz=source_tgz,
                instruction=instruction,
                before_call=ed_before,
                after_call=ed_after,
                on_attempt_failure=ed_fail,
            )
        except PreCallGuardTripped as exc:
            # Budget/wall-clock исчерпан перед/между retry-вызовами editor → авто-rollback.
            await _auto_rollback_edit(session, job, project, base_revision)
            logger.warning("edit_guard", extra={"job_id": job_id, "reason": exc.reason})
            return
        except (AgentOutputError, StructuredOutputError) as exc:
            # Невалидный output editor после ретраев → авто-rollback (правка не применена).
            await _auto_rollback_edit(session, job, project, base_revision)
            logger.warning("edit_invalid_output", extra={"job_id": job_id, "error": str(exc)})
            return

        if result.unrecoverable is not None:
            # Editor явно «неисправимо» → авто-rollback, сайт остаётся LIVE на прежней ревизии.
            await record_event(
                session,
                job.id,
                "edit_unrecoverable",
                payload={"reason": result.unrecoverable.reason},
            )
            await _auto_rollback_edit(session, job, project, base_revision)
            return

        assert result.tree is not None
        source_tgz_new = workspace.pack_source_tgz(result.tree)
        source_ref = await storage.put_bytes(
            s3.source_key(job_id), source_tgz_new, "application/gzip"
        )
        await _create_revision(session, project, job_id, source_ref)
        await record_event(session, job.id, "source_packed", payload={"source_ref": source_ref})
        await transition(
            session,
            job,
            JobState.BUILDING,
            event_type="state_changed",
            payload={"source_ref": source_ref},
        )
        dispatch_for_state(job.id, JobState.BUILDING)


@celery_app.task(name="pipeline.task_edit", queue="llm", bind=True, **_RETRY_KWARGS)
def task_edit(self: Task, job_id: str) -> None:
    # ADR-019 §G: graceful-fail при недоступности LLM (Agent 4 editor) → FAILED(agent_unavailable).
    # requires_llm=True: per-job fail-fast preflight пустого ANTHROPIC_API_KEY (§Fix round 3 п.1).
    run_agent_task(self, lambda: _edit(job_id), job_id, requires_llm=True)


# --- helpers ---

# event_type, под которым в job_events фиксируется назначенный site_id (path-режим).
# Append-only источник истины site_id для джобы между фазами build (--base) и deploy
# (PathPrefix/StripPrefix/live_url): build и deploy — разные Celery-таски, поэтому site_id
# обязан быть стабильным и переживать crash-resume/fix-loop (один job_id → один site_id).
_SITE_ID_ASSIGNED_EVENT = "site_id_assigned"


async def _resolve_site_id(session: AsyncSession, job_id: str) -> str:
    """Стабильный site_id (= site_deployments.subdomain) джобы по режиму routing (ADR-017 §2A).

    path-режим: site_id обязан быть известен ДО build (`vite --base=/s/{site_id}/`) и
    идентичен на deploy (PathPrefix/StripPrefix/live_url/health), иначе ассеты за StripPrefix
    404. build и deploy — разные таски → site_id персистится в append-only job_events
    (_SITE_ID_ASSIGNED_EVENT). Первый вызов (фаза build) генерирует и записывает opaque
    [a-z0-9]{16}; повторные вызовы (deploy, fix-loop rebuild, crash-resume) читают тот же —
    один job_id → один site_id (стабилен, opaque, не реюзается между джобами).

    subdomain-режим: site_id не нужен на build; на deploy генерируется свежий new_subdomain()
    (поведение S1-S6 без изменений) — этот хелпер для subdomain не вызывается.

    Идемпотентно: запись site_id коммитится вызывающим в общей транзакции; повторный вызов
    в той же/новой транзакции находит существующий event и возвращает прежнее значение.
    """
    from app.db.models import JobEvent

    existing = (
        await session.execute(
            select(JobEvent)
            .where(
                JobEvent.job_id == job_id,
                JobEvent.event_type == _SITE_ID_ASSIGNED_EVENT,
            )
            .order_by(JobEvent.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        site_id = existing.payload.get("site_id") if existing.payload else None
        if isinstance(site_id, str) and site_id:
            return site_id

    site_id = new_subdomain()
    await record_event(session, job_id, _SITE_ID_ASSIGNED_EVENT, payload={"site_id": site_id})
    return site_id


async def _abort_if_project_deleted(session: AsyncSession, job: GenerationJob) -> bool:
    """Guard продвигающих тасок против soft-deleted проекта (ADR-011 §C / deploy §6 шаг 1).

    Гонка GC↔in-flight виток: проект мог быть soft-deleted (projects.deleted_at) между
    постановкой таски и её исполнением. Любая продвигающая таска (interview/spec/build/
    deploy/fix) обязана это проверить ПЕРЕД дорогой работой (вызов Claude / docker run),
    иначе джоба удаляемого проекта жжёт токены/ресурсы до guarded-точки.

    Возвращает True, если джоба прервана (проект отсутствует или soft-deleted): в этом
    случае джоба переведена в FAILED(project_missing|project_deleted) и caller обязан
    немедленно return. Частичные ресурсы (если успели создаться) снесёт project.gc.
    Возвращает False, если проект жив — caller продолжает виток.
    """
    project = await session.get(Project, job.project_id)
    if project is None:
        await fail_job(session, job, failure_reason="project_missing")
        logger.info("task_abort_project_missing", extra={"job_id": job.id})
        return True
    if project.deleted_at is not None:
        await fail_job(session, job, failure_reason="project_deleted")
        logger.info("task_abort_project_deleted", extra={"job_id": job.id})
        return True
    return False


async def _finalize_fix_failure(
    session: AsyncSession,
    job: GenerationJob,
    *,
    failure_reason: str,
    signature: str | None,
) -> bool:
    """Финализация неудачи FIXING-витка. Возвращает True, если выполнен авто-rollback (edit).

    Generation (kind != 'edit'): штатный FAILED(failure_reason) → False.
    Edit (kind='edit', ADR-014 §C): авто-rollback на прежнюю good-ревизию +
    FAILED(edit_failed_rolled_back), сайт остаётся LIVE → True.
    """
    if job.kind != "edit":
        await fail_job(
            session, job, failure_reason=failure_reason, last_failure_signature=signature
        )
        return False
    project = await session.get(Project, job.project_id)
    if project is None:
        await fail_job(session, job, failure_reason="project_missing")
        return False
    _, base_revision = await _load_edit_request(session, job.id, project)
    if base_revision is None:
        # Нет прежней good-ревизии для отката — финализируем как edit_failed_rolled_back
        # (сайт уже на прежней good, передеплой не требуется).
        await fail_job(session, job, failure_reason="edit_failed_rolled_back")
        return True
    await _auto_rollback_edit(session, job, project, base_revision)
    return True


async def _load_edit_request(
    session: AsyncSession, job_id: str, project: Project
) -> tuple[str | None, Revision | None]:
    """Инструкция правки + базовая good-ревизия из job_events edit_requested (ADR-014 §A).

    instruction-колонки в data-model нет — источник истины инструкции — append-only
    job_events (edit_service пишет edit_requested). Базовую ревизию берём из payload
    (current good на момент создания edit-джобы), при отсутствии — текущая good проекта.
    """
    from app.db.models import JobEvent

    result = await session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.event_type == "edit_requested")
        .order_by(JobEvent.id)
        .limit(1)
    )
    event = result.scalar_one_or_none()
    if event is None:
        return None, None
    instruction = event.payload.get("instruction") if event.payload else None
    base_revision_id = event.payload.get("base_revision_id") if event.payload else None
    if not isinstance(instruction, str) or not instruction:
        return None, None
    revision = None
    if isinstance(base_revision_id, str):
        revision = await session.get(Revision, base_revision_id)
    if revision is None and project.current_revision_id is not None:
        revision = await session.get(Revision, project.current_revision_id)
    if revision is None or not revision.is_good:
        return instruction, None
    return instruction, revision


async def _auto_rollback_edit(
    session: AsyncSession,
    job: GenerationJob,
    project: Project,
    base_revision: Revision,
) -> None:
    """Авто-rollback неудачной правки (ADR-014 §C): передеплой прежней good-ревизии,
    edit-джоба → FAILED(edit_failed_rolled_back). Сайт остаётся LIVE на прежней ревизии —
    падает ТОЛЬКО edit-джоба.

    Передеплой использует ту же re-deploy-механику, что ручной rollback (deploy §7). Если
    прежняя ревизия уже active (current_revision_id не сдвигался — правка не дошла до
    деплоя), re-deploy не обязателен; всё равно зовём идемпотентно для гарантии.
    """
    from app.deploy.rollback import redeploy_revision

    await record_event(
        session,
        job.id,
        "edit_rolled_back",
        payload={"base_revision_id": base_revision.id},
    )
    await session.commit()

    # Sprint 6 (ADR-015 §2.5): авто-rollback неудачной правки — outcome edit_failed_rolled_back.
    metrics.edit_outcome_total.labels(outcome="edit_failed_rolled_back").inc()

    # Передеплой прежней good-ревизии только если она не текущая active (иначе сайт уже на
    # ней — правка не сменила current_revision_id). Re-deploy в отдельных сессиях.
    if project.current_revision_id != base_revision.id:
        redeploy_result = await redeploy_revision(project.id, base_revision.id, kind="edit")
        metrics.rollback_total.labels(
            trigger="auto_edit_fail",
            result="success" if redeploy_result.ok else "infra_error",
        ).inc()

    # Финализация edit-джобы: FAILED(edit_failed_rolled_back). Перечитываем в свежей сессии,
    # т.к. redeploy_revision коммитил в своих сессиях (project.current_revision_id обновлён).
    refreshed = await load_job(session, job.id)
    if refreshed is not None and refreshed.state != JobState.FAILED:
        await fail_job(session, refreshed, failure_reason="edit_failed_rolled_back")


async def _handle_invalid_patch(
    session: AsyncSession,
    storage: S3Storage,
    job: GenerationJob,
    exc: AgentOutputError | StructuredOutputError,
    revision: Revision | None,
) -> None:
    """Невалидный патч Agent 4 — fix-неудача (docs §A): перезаписать failure_log
    классом agent_output_invalid и снова войти в FIXING (учёт в retry_count/no-progress).

    Так следующий виток task_fix проверит гарды по новой сигнатуре. Если на этом
    витке исчерпан hard cap (retry_count) — гард переведёт в FAILED(build_unrecoverable);
    специфичный invalid_agent_output фиксируется через failure_class лога.

    ADR-020 §I.3: после исчерпания structured-output ретраев Agent 4 пробрасывает либо
    доменный AgentOutputError (с .signature), либо StructuredOutputError (чистый parse-фейл
    без tool_use/JSON — .signature нет). Оба = виток класса agent_output_invalid.
    """
    rule = getattr(exc, "signature", "agent_output_invalid")
    # Перезаписываем источник истины для сигнатуры новым классом agent_output_invalid.
    log = build_failure_log(
        failure_class="agent_output_invalid",
        body=f"agent4 patch rejected: {exc} (rule={rule})",
        revision_no=revision.revision_no if revision is not None else None,
        extra_header={"job_id": job.id},
    )
    # ADR-022: agent_output_invalid → отдельный per-attempt ключ agent.{retry_count}.log
    # (НЕ build_log_key). _handle_invalid_patch не инкрементирует retry_count, поэтому
    # пишется тем же N, что и build/deploy-фейл витка; отдельное имя-стадии исключает
    # затирание их логов того же витка.
    log_ref = await storage.put_text(s3.agent_log_key(job.id, job.retry_count), log, "text/plain")
    job.failure_log_ref = log_ref
    # Новый failure-event (невалидный патч): помечаем для гарда no-progress (§C(d)),
    # чтобы повтор той же agent_output_invalid-сигнатуры на новом витке ловился, а
    # crash-resume того же события — нет.
    job.failure_event_pending = True
    # ADR-029 §Связь с watchdog / pipeline §E2: distinct failure-event витка БЕЗ смены state
    # двигает heartbeat прогресса (last_transition_at) — иначе живая прогрессирующая fix/edit-
    # джоба (LLM-вызовы идут, но state остаётся FIXING) получила бы ложный FAILED(stuck_timeout)
    # от reconciler'а (корень прод-инцидента race FAILED↔LIVE). Гарантия от вечного зацикливания —
    # wall-clock §C(c) / no-progress §C(d), не stuck-таймер.
    await touch_heartbeat(session, job)
    await record_event(
        session,
        job.id,
        "fix_rejected",
        payload={"rule": rule, "failure_log_ref": log_ref},
    )
    # Остаёмся в FIXING и переставляем task_fix: новый виток проверит гарды (no-progress
    # по обновлённой сигнатуре, hard cap). Идемпотентно: state уже FIXING.
    await session.commit()
    dispatch_for_state(job.id, JobState.FIXING)


async def _load_spec(session: AsyncSession, storage: S3Storage, job: GenerationJob) -> str:
    """Финальная спека Agent 2: inline spec_tz или загрузка из S3 по spec_ref (docs §A)."""
    if job.spec_tz is not None:
        return job.spec_tz
    if job.spec_ref is not None:
        return (await storage.get_bytes(job.spec_ref)).decode("utf-8")
    return ""


def _classify_build_failure(build_log: str) -> str:
    """Классифицирует build-fail в машинный failure_class (§F): npm_install vs build."""
    lowered = build_log.lower()
    if "npm err" in lowered:
        return "npm_install_error"
    return "build_error"


def _classify_health_failure(detail: str) -> str:
    """Классифицирует health-fail в машинный failure_class (§F) по детали health-check."""
    lowered = detail.lower()
    if "status 5" in lowered:
        return "health_5xx"
    if "status 4" in lowered:
        return "health_4xx"
    return "health_timeout"


async def _load_qa_pairs(session: AsyncSession, job_id: str) -> list[tuple[str, str]]:
    questions = (
        (
            await session.execute(
                select(Question).where(Question.job_id == job_id).order_by(Question.position)
            )
        )
        .scalars()
        .all()
    )
    answers = (await session.execute(select(Answer).where(Answer.job_id == job_id))).scalars().all()
    answer_by_q = {a.question_id: a.text for a in answers}
    return [(q.text, answer_by_q.get(q.id, "")) for q in questions]


async def _create_revision(
    session: AsyncSession, project: Project, job_id: str, source_ref: str
) -> Revision:
    last_no = (
        await session.execute(
            select(Revision.revision_no)
            .where(Revision.project_id == project.id)
            .order_by(Revision.revision_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    revision_no = (last_no or 0) + 1
    revision = Revision(
        id=new_revision_id(),
        project_id=project.id,
        revision_no=revision_no,
        source_artifact_ref=source_ref,
        created_from_job_id=job_id,
        is_good=True,
    )
    session.add(revision)
    return revision


def _pack_dir(directory: Path) -> bytes:
    """Упаковывает каталог dist/ в .tgz (regular files), сохраняя относительные пути."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(directory).as_posix()
                info = tarfile.TarInfo(name=arcname)
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                info.type = tarfile.REGTYPE
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
