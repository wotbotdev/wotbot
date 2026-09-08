from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


def _bounded_links(values: Any) -> tuple[dict[str, str], ...]:
    links: list[dict[str, str]] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        if not isinstance(value, dict):
            continue
        url = str(value.get("url") or "").strip()
        if len(url) > 4096:
            continue
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            continue
        links.append(
            {
                "title": str(value.get("title") or "Link")[:200],
                "url": url,
                "media_type": str(value.get("media_type") or "")[:200],
            }
        )
        if len(links) == 6:
            break
    return tuple(links)


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    provider: str
    title: str
    external_id: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    network_access: Literal["public", "private"] = "public"
    config: dict[str, Any] = field(default_factory=dict)
    security_name: str = "nosec_sc"
    security_scheme: str = "nosec"
    credential: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def get(self, name: str, default: Any = "") -> Any:
        """Return one provider-owned configuration value."""

        return self.config.get(name, default)


@dataclass(frozen=True, slots=True)
class SearchIntent:
    """Provider-neutral search intent derived without another model call."""

    original: str
    entities: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.entities, *self.keywords)))


@dataclass(frozen=True, slots=True)
class ProviderConfigSpec:
    fields: frozenset[str]
    url_fields: tuple[str, ...] = ()
    optional_url_fields: tuple[str, ...] = ()
    text_defaults: tuple[tuple[str, str], ...] = ()
    float_defaults: tuple[tuple[str, float], ...] = ()
    requires_secret: bool = False
    default_security_scheme: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    document: dict[str, Any]
    warnings: tuple[str, ...] = ()
    # Service endpoints the dataset points at but this provider cannot model.
    # These are URLs to hand to source detection, not affordances onboarding
    # resolved; each carries "url" and a human label.
    suggested_sources: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshRecord:
    user_id: str
    thing_id: str
    source_id: str
    provider: str
    thing_document_hash: str
    source_hash: str
    document: dict[str, Any]
    credentials_to_remove: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RefreshRecord:
        return cls(
            user_id=str(value["user_id"]),
            thing_id=str(value["thing_id"]),
            source_id=str(value["source_id"]),
            provider=str(value["provider"]),
            thing_document_hash=str(value["thing_document_hash"]),
            source_hash=str(value["source_hash"]),
            document=dict(value["document"]),
            credentials_to_remove=tuple(
                str(item) for item in value.get("credentials_to_remove", ())
            ),
            warnings=tuple(str(item) for item in value.get("warnings", ())),
        )


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    provider: str
    source_id: str
    external_id: str
    kind: str
    title: str
    summary: str = ""
    links: tuple[dict[str, str], ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ("onboard",)


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    scope_kind: Literal["thread"]
    scope_id: str
    provider: str
    source_id: str
    external_id: str
    kind: str
    title: str
    summary: str
    links: tuple[dict[str, str], ...]
    payload: dict[str, Any]
    capabilities: tuple[str, ...]

    @classmethod
    def from_draft(
        cls,
        draft: CandidateDraft,
        *,
        scope_kind: Literal["thread"],
        scope_id: str,
    ) -> CandidateRecord:
        return cls(
            scope_kind=scope_kind,
            scope_id=scope_id,
            provider=draft.provider,
            source_id=draft.source_id,
            external_id=draft.external_id,
            kind=draft.kind,
            title=draft.title[:500],
            summary=draft.summary[:2000],
            links=_bounded_links(draft.links),
            payload=dict(draft.payload),
            capabilities=tuple(draft.capabilities),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateRecord:
        return cls(
            scope_kind=str(value["scope_kind"]),  # type: ignore[arg-type]
            scope_id=str(value["scope_id"]),
            provider=str(value["provider"]),
            source_id=str(value["source_id"]),
            external_id=str(value["external_id"]),
            kind=str(value["kind"]),
            title=str(value["title"])[:500],
            summary=str(value.get("summary", ""))[:2000],
            links=_bounded_links(value.get("links", ())),
            payload=dict(value.get("payload", {})),
            capabilities=tuple(str(item) for item in value.get("capabilities", ())),
        )

    def public(self, candidate_id: str) -> dict[str, Any]:
        return {
            "candidate_id": candidate_id,
            "source": self.source_id,
            "provider": self.provider,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "capabilities": list(self.capabilities),
            "links": list(self.links),
        }


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    endpoint: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    public: bool = False
    content_type: str = "application/octet-stream"
    filename: str = "download"
    size_bytes: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DownloadRecord:
        return cls(
            endpoint=str(value["endpoint"]),
            headers={
                str(name): str(header_value)
                for name, header_value in dict(value.get("headers") or {}).items()
            },
            public=bool(value.get("public", False)),
            content_type=str(value.get("content_type") or "application/octet-stream"),
            filename=str(value.get("filename") or "download"),
            size_bytes=(
                int(value["size_bytes"])
                if isinstance(value.get("size_bytes"), int)
                and not isinstance(value.get("size_bytes"), bool)
                and value["size_bytes"] >= 0
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    body: bytes = field(repr=False)
    content_type: str = "application/json"
