"""Unit: S3Storage.delete_prefix — per-object delete_object + пагинация (#4 project_gc).

Мокается aioboto3-клиент на границе (никакой реальной сети/MinIO). Проверяет:
7. delete_prefix удаляет все объекты под префиксом через per-object delete_object —
   batch delete_objects (требующий Content-MD5 → MissingContentMD5 на MinIO) НЕ вызывается;
8. идемпотентность: повторный delete_prefix по пустому префиксу = no-op (deleted=0);
9. пагинация: >batch_size объектов под префиксом удаляются полностью (несколько страниц
   list_objects_v2 с PageSize=batch_size).

Нормативный источник: app/storage/s3.py delete_prefix docstring (per-object delete,
MissingContentMD5-мотивация), docs/modules/deploy/03-architecture.md §F-1 §Ретеншн.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.storage.s3 import S3Storage

pytestmark = pytest.mark.asyncio


class _FakePaginator:
    def __init__(self, keys: list[str], page_size: int) -> None:
        self._keys = keys
        self._page_size = page_size
        self.paginate_kwargs: dict | None = None

    def paginate(self, **kwargs):  # noqa: ANN003, ANN202
        self.paginate_kwargs = kwargs
        prefix = kwargs["Prefix"]
        matched = [k for k in self._keys if k.startswith(prefix)]
        page_size = self._page_size

        async def _gen():  # noqa: ANN202
            if not matched:
                # Реальный S3-пагинатор отдаёт одну пустую страницу без Contents.
                yield {}
                return
            for i in range(0, len(matched), page_size):
                yield {"Contents": [{"Key": k} for k in matched[i : i + page_size]]}

        return _gen()


class _FakeClient:
    """Фейк aioboto3 S3-клиента: paginator list_objects_v2 + delete_object/delete_objects-счёт."""

    def __init__(self, keys: list[str], page_size_holder: dict) -> None:
        self._store = set(keys)
        self._page_size_holder = page_size_holder
        self.delete_object_calls: list[str] = []
        self.delete_objects_calls: list[dict] = []  # batch DeleteObjects — НЕ должен вызываться
        self.last_paginator: _FakePaginator | None = None

    def get_paginator(self, name: str) -> _FakePaginator:  # noqa: ANN202
        assert name == "list_objects_v2"
        # page_size извлекается из paginate(PaginationConfig=...) — заполняется при вызове.
        pag = _FakePaginator(sorted(self._store), self._page_size_holder["page_size"])
        self.last_paginator = pag
        return pag

    async def delete_object(self, *, Bucket, Key):  # noqa: ANN001, ANN003, ANN202, N803
        self.delete_object_calls.append(Key)
        self._store.discard(Key)

    async def delete_objects(self, **kwargs):  # noqa: ANN003, ANN202
        # Batch DeleteObjects (требует Content-MD5 на MinIO) — НЕ должен использоваться.
        self.delete_objects_calls.append(kwargs)
        raise AssertionError("delete_objects (batch) не должен вызываться — только per-object")

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *a):  # noqa: ANN002, ANN202
        return False


class _FakeSession:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def client(self, **kwargs):  # noqa: ANN003, ANN202
        return self._client


def _make_storage(keys: list[str], page_size_holder: dict) -> tuple[S3Storage, _FakeClient]:
    storage = S3Storage(get_settings())
    client = _FakeClient(keys, page_size_holder)
    # Подменяем aioboto3-сессию на фейк (граница S3 изолирована).
    storage._session = _FakeSession(client)
    return storage, client


# --- #7: per-object delete всех объектов под префиксом, без batch DeleteObjects ---


async def test_delete_prefix_uses_per_object_delete_no_batch():
    keys = [
        "logs/j_aaa/build.0.log",
        "logs/j_aaa/build.1.log",
        "logs/j_aaa/deploy.0.log",
        "sources/j_aaa/source.tgz",  # вне префикса logs/j_aaa/
    ]
    holder = {"page_size": 1000}
    storage, client = _make_storage(keys, holder)

    deleted = await storage.delete_prefix("logs/j_aaa/", batch_size=1000)

    assert deleted == 3, "удалены ровно 3 объекта под logs/j_aaa/"
    # Per-object delete_object для каждого ключа под префиксом.
    assert sorted(client.delete_object_calls) == [
        "logs/j_aaa/build.0.log",
        "logs/j_aaa/build.1.log",
        "logs/j_aaa/deploy.0.log",
    ]
    # Batch DeleteObjects НЕ вызывался (иначе MissingContentMD5 на MinIO).
    assert client.delete_objects_calls == []
    # Объект вне префикса не тронут.
    assert "sources/j_aaa/source.tgz" in client._store


# --- #8: идемпотентность — пустой префикс → no-op (deleted=0) ---


async def test_delete_prefix_empty_is_noop():
    holder = {"page_size": 1000}
    storage, client = _make_storage([], holder)

    deleted = await storage.delete_prefix("logs/j_missing/", batch_size=1000)

    assert deleted == 0
    assert client.delete_object_calls == []
    assert client.delete_objects_calls == []


async def test_delete_prefix_second_run_after_clear_is_noop():
    """Повторный GC по уже вычищенному префиксу → 0 (idempotent, ADR-022 §3 / ADR-011)."""
    keys = ["logs/j_bbb/build.0.log", "logs/j_bbb/agent.0.log"]
    holder = {"page_size": 1000}
    storage, client = _make_storage(keys, holder)

    first = await storage.delete_prefix("logs/j_bbb/", batch_size=1000)
    assert first == 2
    # Хранилище фейка теперь пусто под префиксом → повтор no-op.
    second = await storage.delete_prefix("logs/j_bbb/", batch_size=1000)
    assert second == 0


# --- #9: пагинация — >batch_size объектов удаляются полностью ---


async def test_delete_prefix_paginates_and_deletes_all_over_batch_size():
    # 25 объектов под префиксом, batch_size=10 → 3 страницы (10+10+5).
    keys = [f"logs/j_ccc/build.{i}.log" for i in range(25)]
    batch_size = 10
    holder = {"page_size": batch_size}
    storage, client = _make_storage(keys, holder)

    deleted = await storage.delete_prefix("logs/j_ccc/", batch_size=batch_size)

    assert deleted == 25, "все объекты со всех страниц удалены"
    assert len(client.delete_object_calls) == 25
    assert set(client.delete_object_calls) == set(keys)
    # PageSize проброшен в list_objects_v2 как ограничитель страницы листинга (MaxKeys).
    assert client.last_paginator.paginate_kwargs["PaginationConfig"] == {"PageSize": batch_size}
    assert client.last_paginator.paginate_kwargs["Prefix"] == "logs/j_ccc/"
