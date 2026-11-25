from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from wotbot.api_keys.models import ApiKeyRecord
from wotbot.api_keys.store import create_api_key, list_api_keys, revoke_api_key
from wotbot.auth.models import User
from wotbot.core.scopes import VALID_SCOPES
from wotbot.workspaces.binding import workspaces_enabled
from wotbot.workspaces.context import DEFAULT_WORKSPACE_ID
from wotbot.workspaces.models import Workspace


def _reject_invalid_scopes(scopes: list[str]) -> None:
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scopes: {', '.join(sorted(invalid))}",
        )


class ApiKeyManagementService:
    def __init__(self, session: Session):
        self._session = session

    def create_for_user(
        self,
        *,
        user: User,
        name: str,
        scopes: list[str],
        expires_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> tuple[ApiKeyRecord, str]:
        _reject_invalid_scopes(scopes)
        target_workspace = self._resolve_workspace(user, workspace_id)

        if user.scopes is not None:
            allowed_scopes = set(user.scopes)
            missing_scopes = set(scopes) - allowed_scopes
            if missing_scopes:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Cannot create API key with scopes you do not have: "
                        + ", ".join(sorted(missing_scopes))
                    ),
                )

        try:
            return create_api_key(
                self._session,
                user_id=user.user_id,
                name=name,
                scopes=scopes,
                expires_at=expires_at,
                workspace_id=target_workspace,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _resolve_workspace(self, user: User, requested: str | None) -> str:
        """New keys stay in the creator's workspace unless it may manage workspaces."""
        own = user.workspace_id or DEFAULT_WORKSPACE_ID
        if requested is None or requested == own:
            return own
        if not workspaces_enabled():
            raise HTTPException(status_code=422, detail="Workspaces are not enabled")
        if user.scopes is not None and "workspaces:manage" not in user.scopes:
            raise HTTPException(
                status_code=403,
                detail="Creating a key for another workspace requires the workspaces:manage scope",
            )
        if self._session.get(Workspace, requested) is None:
            raise HTTPException(status_code=422, detail=f"Unknown workspace '{requested}'")
        return requested

    def list_for_user(self, user_id: str) -> list[ApiKeyRecord]:
        return list_api_keys(self._session, user_id)

    def revoke_for_user(self, key_id: str, user_id: str) -> None:
        revoked = revoke_api_key(self._session, key_id, user_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="API key not found")
