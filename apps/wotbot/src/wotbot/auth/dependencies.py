from fastapi import Depends, HTTPException, status

from wotbot.auth.models import User
from wotbot.auth.providers import get_current_user
from wotbot.workspaces.binding import bind_principal_workspace


async def require_user(current_user: User | None = Depends(get_current_user)) -> User:
    # Async on purpose: FastAPI runs it in the request task, so the workspace
    # bound here is inherited by the endpoint and by any thread it starts.
    # A sync dependency would run in a worker thread whose context is discarded.
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    bind_principal_workspace(current_user)
    return current_user


def require_scopes(required: list[str]):
    def _check(user: User = Depends(require_user)) -> User:
        missing = set(required) - set(user.scopes or [])
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scopes: {', '.join(sorted(missing))}",
            )
        return user

    return _check


def require_service(allowed_services: list[str] | None = None):
    def _check(user: User = Depends(require_user)) -> User:
        if user.auth_type != "service" or not user.service_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service authentication required",
            )
        if allowed_services is not None and user.service_id not in allowed_services:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service is not allowed to access this endpoint",
            )
        return user

    return _check
