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
        """Batch-delete всех объектов под key-префиксом. Возвращает число удалённых ключей.

        Используется project.gc (ADR-011): снос всех S3-артефактов проекта по префиксам
        sources/dist/logs/specs всех его job_id. Идемпотентно: отсутствие объектов под
        префиксом → 0 (повторный GC — no-op). Пагинация list_objects_v2 + delete_objects
        батчами batch_size (S3 DeleteObjects лимит — 1000 ключей за запрос).
        """
        deleted = 0
        async with self._session.client(**self._client_kwargs()) as client:
            paginator = client.get_paginator("list_objects_v2")
            batch: list[dict[str, str]] = []
            async for page in paginator.paginate(Bucket=self._settings.s3_bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})
                    if len(batch) >= batch_size:
                        await client.delete_objects(
                            Bucket=self._settings.s3_bucket,
                            Delete={"Objects": batch, "Quiet": True},
                        )
                        deleted += len(batch)
                        batch = []
            if batch:
                await client.delete_objects(
                    Bucket=self._settings.s3_bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                deleted += len(batch)
        return deleted


def get_storage() -> S3Storage:
    return S3Storage(get_settings())


# Ключи артефактов (детерминированно).


def source_key(job_id: str) -> str:
    return f"sources/{job_id}/source.tgz"


def dist_key(job_id: str) -> str:
    return f"dist/{job_id}/dist.tgz"


def build_log_key(job_id: str) -> str:
    return f"logs/{job_id}/build.log"


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
