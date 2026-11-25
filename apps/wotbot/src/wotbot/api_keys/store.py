import hashlib
import secrets
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from wotbot.api_keys.models import ApiKey, ApiKeyRecord, ApiKeyRow
from wotbot.core.scopes import API_KEY_SCOPES, VALID_SCOPES
from wotbot.core.time import utc_now
from wotbot.workspaces.context import DEFAULT_WORKSPACE_ID


def _validate_scopes(scopes: list[str]) -> None:
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise ValueError(f"Invalid scopes: {', '.join(sorted(invalid))}")


def generate_api_key() -> str:
    return "slc_" + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _row_from_model(row: ApiKey) -> ApiKeyRow:
    return ApiKeyRow(
        id=row.id,
        key_prefix=row.key_prefix,
        key_hash=row.key_hash,
        name=row.name,
        scopes=list(row.scopes or []),
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        is_active=row.is_active,
        workspace_id=row.workspace_id,
    )


def _to_record(row: ApiKeyRow) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row.id,
        key_prefix=row.key_prefix,
        name=row.name,
        scopes=list(row.scopes or []),
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        is_active=row.is_active,
        workspace_id=row.workspace_id,
    )


def create_api_key(
    session: Session,
    user_id: str,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> tuple[ApiKeyRecord, str]:
    _validate_scopes(scopes)

    raw_key = generate_api_key()
    now = utc_now()
    row = ApiKey(
        id=str(uuid.uuid4()),
        key_prefix=raw_key[:12],
        key_hash=hash_api_key(raw_key),
        name=name,
        scopes=list(scopes),
        user_id=user_id,
        workspace_id=workspace_id,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    session.commit()
    return _to_record(_row_from_model(row)), raw_key


def list_api_keys(session: Session, user_id: str) -> list[ApiKeyRecord]:
    rows = session.scalars(
        select(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True))
        .order_by(ApiKey.created_at.desc())
    ).all()
    return [_to_record(_row_from_model(row)) for row in rows]


def revoke_api_key(session: Session, key_id: str, user_id: str) -> bool:
    row = session.scalar(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.is_active.is_(True),
        )
    )
    if row is None:
        return False

    row.is_active = False
    row.updated_at = utc_now()
    session.commit()
    return True


def lookup_api_key_by_hash(
    session: Session,
    key_hash: str,
) -> ApiKeyRow | None:
    row = session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    return _row_from_model(row) if row is not None else None


def touch_last_used(session: Session, row: ApiKeyRow) -> None:
    stored = session.get(ApiKey, row.id)
    if stored is None:
        return
    stored.last_used_at = utc_now()
    session.commit()


def ensure_init_admin_key(
    session: Session,
    raw_token: str,
    user_id: str,
) -> bool:
    """Create or refresh the all-scopes API key for *raw_token*.

    Returns True if a new row was inserted, False if it was already present.
    """
    key_hash = hash_api_key(raw_token)
    scopes = list(API_KEY_SCOPES)
    now = utc_now()
    row = session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    if row is None:
        session.add(
            ApiKey(
                id=str(uuid.uuid4()),
                key_prefix=raw_token[:12],
                key_hash=key_hash,
                name="Init Admin Token",
                scopes=scopes,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        return True

    row.name = "Init Admin Token"
    row.scopes = scopes
    row.user_id = user_id
    row.is_active = True
    row.updated_at = now
    session.commit()
    return False
