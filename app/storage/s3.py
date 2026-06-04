"""Async S3/MinIO клиент поверх aioboto3 (docs/02-tech-stack.md).

Хранит source.tgz, dist/, build-логи. В БД — только S3-ключи (*_ref).
Ключи строятся детерминированно по job_id/revision.
"""

from __future__ import annotations

import aioboto3

from app.core.config import Settings, get_settings


class S3Storage:
    """Тонкая обёртка над aioboto3 для put/get объектов."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()

    def _client_kwargs(self) -> dict[str, object]:
        s = self._settings
        kwargs: dict[str, object] = {
            "service_name": "s3",
            "region_name": s.s3_region,
            "aws_access_key_id": s.s3_access_key.get_secret_value(),
            "aws_secret_access_key": s.s3_secret_key.get_secret_value(),
            "use_ssl": s.s3_use_ssl,
        }
        if s.s3_endpoint_url:
            kwargs["endpoint_url"] = s.s3_endpoint_url
        return kwargs

    async def ensure_bucket(self) -> None:
        """Создаёт бакет, если его нет (idempotent, для dev/MinIO)."""
        async with self._session.client(**self._client_kwargs()) as client:
            try:
                await client.head_bucket(Bucket=self._settings.s3_bucket)
            except client.exceptions.ClientError:
                await client.create_bucket(Bucket=self._settings.s3_bucket)

    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """Кладёт объект, возвращает ключ (он же *_ref в БД)."""
        async with self._session.client(**self._client_kwargs()) as client:
            await client.put_object(
                Bucket=self._settings.s3_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return key

    async def get_bytes(self, key: str) -> bytes:
        async with self._session.client(**self._client_kwargs()) as client:
            resp = await client.get_object(Bucket=self._settings.s3_bucket, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()

    async def put_text(self, key: str, text: str, content_type: str = "text/plain") -> str:
        return await self.put_bytes(key, text.encode("utf-8"), content_type)

    async def delete_prefix(self, prefix: str, *, batch_size: int) -> int:
        """Per-object delete всех объектов под key-префиксом. Возвращает число удалённых ключей.

        Используется project.gc (ADR-011): снос всех S3-артефактов проекта по префиксам
        sources/dist/logs/specs всех его job_id (включая per-attempt build/deploy/agent-логи
        ADR-022 под logs/{job_id}/). Идемпотентно: отсутствие объектов под префиксом → 0
        (повторный GC — no-op).

        Удаление — per-object `delete_object` в цикле (НЕ batch `delete_objects`): MinIO
        требует Content-MD5 на batch DeleteObjects, которого boto3 в текущей конфигурации не
        шлёт (botocore ClientError 'MissingContentMD5' → project.gc падал, S3-артефакты не
        дочищались). Single `delete_object` Content-MD5 не требует и совместим и с MinIO, и с
        S3. Объёмы GC умеренные (единицы–десятки объектов на job: sources/dist/spec + per-
        attempt логи, ограниченные max_fix_attempts), так что per-object delete приемлем.

        `batch_size` ограничивает размер страницы list_objects_v2 (MaxKeys) — паджинация
        листинга, не гранулярность удаления. Идемпотентность сохранена: delete_object на
        отсутствующий ключ S3/MinIO трактуют как успех (повторный GC безопасен).
        """
        deleted = 0
        bucket = self._settings.s3_bucket
        async with self._session.client(**self._client_kwargs()) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": batch_size}
            ):
                for obj in page.get("Contents", []):
                    await client.delete_object(Bucket=bucket, Key=obj["Key"])
                    deleted += 1
        return deleted


def get_storage() -> S3Storage:
    return S3Storage(get_settings())


# Ключи артефактов (детерминированно).


def source_key(job_id: str) -> str:
    return f"sources/{job_id}/source.tgz"


def dist_key(job_id: str) -> str:
    return f"dist/{job_id}/dist.tgz"


def build_log_key(job_id: str, retry_count: int) -> str:
    """Per-attempt build-лог (ADR-022): успех и build_error/npm_install_error витка.

    Дискриминатор — монотонный generation_jobs.retry_count (0 на первой сборке,
    +1 на входе FIXING→BUILDING). Уникальный ключ на попытку → ранний лог не затирается.
    """
    return f"logs/{job_id}/build.{retry_count}.log"


def deploy_log_key(job_id: str, retry_count: int) -> str:
    """Per-attempt deploy/health-лог (ADR-022): deploy_error/health_* витка.

    Отдельное имя-стадии при том же retry_count, что и build.{n}: deploy-фейл витка N
    не затирает лог успешной сборки того же витка (build.{n}.log).
    """
    return f"logs/{job_id}/deploy.{retry_count}.log"


def agent_log_key(job_id: str, retry_count: int) -> str:
    """Per-attempt agent-reject-лог (ADR-022): agent_output_invalid витка.

    _handle_invalid_patch НЕ инкрементирует retry_count → пишется с тем же N, что и
    build/deploy-фейл витка. Отдельное имя-стадии исключает затирание их логов.
    """
    return f"logs/{job_id}/agent.{retry_count}.log"


def spec_key(job_id: str) -> str:
    return f"specs/{job_id}/spec.md"


def job_artifact_prefixes(job_id: str) -> list[str]:
    """Все key-префиксы артефактов одного job_id (ADR-011 §B.4 / docs/07 модель хранения).

    sources/{job_id}/, dist/{job_id}/, logs/{job_id}/, specs/{job_id}/ — для batch-delete
    в project.gc по всем job_id проекта. Слэш в конце — точный per-job префикс (без
    случайного захвата соседних job_id с общим строковым началом).
    """
    return [
        f"sources/{job_id}/",
        f"dist/{job_id}/",
        f"logs/{job_id}/",
        f"specs/{job_id}/",
    ]
