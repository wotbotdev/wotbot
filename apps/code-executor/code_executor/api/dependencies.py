"""FastAPI dependencies: auth and app state access."""

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer()


def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    settings = request.app.state.settings
    if not settings or credentials.credentials != settings.internal_api_key:
        raise HTTPException(status_code=401, detail="Invalid internal API key")
    return credentials.credentials
