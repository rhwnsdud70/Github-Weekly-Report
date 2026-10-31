"""Observation storage DTOs only; no transport or database implementation."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from models.observation import Integrity, Producer
if TYPE_CHECKING:
    from services.observation_pool import PoolResult


@dataclass(frozen=True)
class PoolPayload:
    pool_id: str
    mode: str
    fixture: bool
    period_start: datetime
    boundary_end: datetime
    created_at: datetime
    producer: Producer
    manifest: dict
    publication_status: str = 'UNCONFIRMED'


@dataclass(frozen=True)
class PoolEnvelope:
    payload: PoolPayload
    integrity: Integrity
    schema_version: int = 1


@dataclass(frozen=True)
class PoolValidation:
    envelope: PoolEnvelope
    pool: PoolResult


@dataclass(frozen=True)
class ObjectReference:
    kind: str
    namespace: str
    boundary: datetime
    object_id: str
    object_key: str
    last_modified: datetime | None = None


@dataclass(frozen=True)
class StoredObject:
    reference: ObjectReference
    validation: object
    byte_size: int


@dataclass(frozen=True)
class PublicationReceipt:
    object_key: str
    object_id: str
    sha256: str
    schema_version: int
    byte_size: int | None
    boundary: datetime
    publication_status: str
    verification_result: str
    error_category: str | None = None
    http_status: int | None = None
