from fastapi import APIRouter, Depends

from wotbot.auth import User, require_user
from wotbot.workspaces.binding import workspaces_enabled

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
def get_me(user: User = Depends(require_user)) -> dict[str, object]:
    identity: dict[str, object] = {
        "user_id": user.user_id,
        "email": user.email,
        "preferred_username": user.preferred_username,
        "groups": user.groups,
        "scopes": user.scopes or [],
    }
    if workspaces_enabled():
        identity["workspace_id"] = user.workspace_id
    return identity
