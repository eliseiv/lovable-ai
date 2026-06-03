"""Хранилище объектов: S3/MinIO. В Postgres — только ссылки (docs/03-data-model.md)."""

from app.storage.s3 import S3Storage, get_storage

__all__ = ["S3Storage", "get_storage"]
