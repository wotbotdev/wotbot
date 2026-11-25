from dataclasses import dataclass, field


@dataclass
class User:
    user_id: str
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    preferred_username: str | None = None
    scopes: list[str] | None = None
    auth_type: str = "user"
    service_id: str | None = None
    api_key_id: str | None = None
    # The workspace an API key is bound to; None for services and anonymous use.
    workspace_id: str | None = None
