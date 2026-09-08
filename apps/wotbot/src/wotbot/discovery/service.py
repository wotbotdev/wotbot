from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
from dataclasses import replace
from typing import Any

import aiohttp
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wotbot.catalog import serialize_thing, validate_document
from wotbot.catalog.models import ThingRecord
from wotbot.catalog.service import ThingCatalogWriteService
from wotbot.catalog.store import get_thing, get_thing_by_origin
from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.discovery.errors import (
    CredentialChallengeError,
    ProviderError,
    RefreshConflictError,
    SourceAuthenticationError,
    SourceConfigurationError,
    SourceConflictError,
    SourceUnavailableError,
    StaleCandidateError,
)
from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import (
    CandidateDraft,
    CandidateRecord,
    RefreshRecord,
    SourceDefinition,
)
from wotbot.discovery.providers import (
    PROVIDERS,
    resolve_private_toolhive_source,
    resolve_public_source,
)
from wotbot.discovery.providers.base import is_http_endpoint, provider_action_href
from wotbot.discovery.search import prepare_search_intent, relevance_score
from wotbot.discovery.source_models import SourceRecord
from wotbot.discovery.source_store import (
    count_source_dependents,
    credential_schemes,
    delete_source,
    delete_source_credentials,
    dependent_counts,
    get_source,
    get_source_by_identity,
    get_source_credential,
    insert_source,
    list_sources,
    search_sources_page,
    update_source,
)
from wotbot.discovery.store import CandidateStore, DownloadStore, RefreshStore

_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._candidate_store = CandidateStore(
            settings.redis_url, ttl_seconds=settings.discovery_candidate_ttl_seconds
        )
        self._download_store = DownloadStore(
            settings.redis_url, ttl_seconds=settings.discovery_download_ttl_seconds
        )
        self._refresh_store = RefreshStore(
            settings.redis_url, ttl_seconds=settings.discovery_refresh_ttl_seconds
        )

    async def search_sources(self, *, query: str = "", limit: int = 10) -> dict[str, Any]:
        bounded_limit = _validate_limit(limit)
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 500:
            raise ValueError("Source search query must contain at most 500 characters")
        records = await asyncio.to_thread(self._list_source_records)
        if not normalized_query:
            selected = records[:bounded_limit]
        else:
            neutral = SourceDefinition(id="", provider="", title="")
            intent = prepare_search_intent(normalized_query, neutral)
            ranked = [
                (
                    relevance_score(
                        intent,
                        record.title,
                        " ".join(
                            (
                                record.title,
                                record.description,
                                " ".join(record.tags),
                                record.provider,
                            )
                        ),
                    ),
                    index,
                    record,
                )
                for index, record in enumerate(records)
            ]
            ranked.sort(key=lambda item: (-item[0], item[1]))
            matched = [record for score, _index, record in ranked if score > 0]
            # Fall back to the ranked registry when nothing matches lexically.
            # A source is named by whatever metadata detection scraped from its
            # homepage, which often omits the word a user would reach for --
            # the Luxembourg portal registers as "Home - Portail Open Data" --
            # so hard-filtering hid the whole registry behind a vocabulary
            # mismatch and stranded the request with nothing to search.
            selected = (matched or [record for _score, _index, record in ranked])[:bounded_limit]
        return {
            "query": normalized_query,
            "items": await asyncio.to_thread(_public_sources, selected),
        }

    async def list_registered_sources(
        self,
        *,
        query: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        bounded_page = max(page, 1)
        bounded_per_page = max(per_page, 1)

        def read() -> dict[str, Any]:
            session_factory = get_session_factory()
            with session_factory() as session:
                records, total = search_sources_page(
                    session,
                    query=query,
                    offset=(bounded_page - 1) * bounded_per_page,
                    limit=bounded_per_page,
                )
                return {
                    "items": _management_sources(session, records),
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                }

        return await asyncio.to_thread(read)

    async def get_registered_source(self, source_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(self._find_source, source_id)
        if record is None:
            raise ValueError("Discovery source was not found")
        return await asyncio.to_thread(_management_source, record)

    async def discover(
        self,
        *,
        source_id: str,
        query: str,
        limit: int,
        thread_id: str,
    ) -> dict[str, Any]:
        record = await asyncio.to_thread(self._find_source, source_id)
        if record is None:
            return _source_unavailable(source_id, "The discovery source is no longer registered.")
        try:
            source, public_http = await asyncio.to_thread(self._source_runtime, record)
            provider = PROVIDERS.get(source.provider)
            if provider is None:
                return _source_unavailable(source_id, "The discovery provider is unavailable.")
            intent = prepare_search_intent(_validate_query(query), source)
            drafts = await provider.search(
                source,
                intent,
                _validate_limit(limit),
                public_http=public_http,
            )
            items = await self._store_many(
                drafts,
                thread_id=thread_id,
                source_id=record.id,
                provider=record.provider,
            )
            return {"source_id": source_id, "query": query, "items": items}
        except SourceAuthenticationError as exc:
            raise CredentialChallengeError(
                status="credential_rejected",
                source_id=record.id,
                security_name=record.security_name,
                scheme=record.security_scheme,
                message="The external source rejected its stored credential.",
            ) from exc
        except CredentialChallengeError:
            raise
        except SourceConfigurationError:
            logger.warning(
                "Discovery source %s has an unusable stored configuration",
                source_id,
                exc_info=True,
            )
            return _source_misconfigured(source_id)
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError):
            logger.warning(
                "Discovery search failed for source %s (provider %s)",
                source_id,
                record.provider,
                exc_info=True,
            )
            return _source_unavailable(
                source_id,
                "The external source is unavailable or returned an invalid response.",
            )

    async def onboard(self, *, candidate_id: str, thread_id: str) -> dict[str, Any]:
        candidate = await self._candidate_store.get(
            candidate_id,
            scope_kind="thread",
            scope_id=thread_id,
        )
        if candidate.kind == "source" or "onboard" not in candidate.capabilities:
            raise ValueError("Candidate cannot be onboarded as a resource")
        source_record = await asyncio.to_thread(self._find_source, candidate.source_id)
        if source_record is None:
            return _source_unavailable(
                candidate.source_id,
                "The candidate's discovery source is no longer registered.",
            )
        if candidate.provider != source_record.provider:
            raise ValueError("Candidate belongs to a different provider")
        try:
            source, _public_http = await asyncio.to_thread(self._source_runtime, source_record)
            return await self._onboard_resource(
                source_record=source_record,
                source=source,
                candidate=_candidate_draft(candidate),
            )
        except SourceAuthenticationError as exc:
            raise CredentialChallengeError(
                status="credential_rejected",
                source_id=source_record.id,
                security_name=source_record.security_name,
                scheme=source_record.security_scheme,
                message="The external source rejected its stored credential.",
            ) from exc
        except CredentialChallengeError:
            raise
        except StaleCandidateError as exc:
            return {
                "status": "stale_candidate",
                "candidate_id": candidate_id,
                "message": str(exc),
            }
        except SourceConfigurationError:
            logger.warning(
                "Discovery source %s has an unusable stored configuration",
                source_record.id,
                exc_info=True,
            )
            return _source_misconfigured(source_record.id)
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError):
            logger.warning(
                "Onboarding failed for candidate from source %s (provider %s)",
                source_record.id,
                source_record.provider,
                exc_info=True,
            )
            return _source_unavailable(
                source_record.id,
                "The external source is unavailable or returned an invalid response.",
            )

    async def register_source(
        self,
        *,
        provider: str,
        title: str,
        description: str,
        tags: list[str],
        config: dict[str, Any],
        security: dict[str, Any] | None,
        network_access: str,
        allow_onboarding: bool = True,
    ) -> dict[str, Any]:
        source = _registered_source(
            provider=provider,
            title=title,
            description=description,
            tags=tags,
            config=config,
            security=security,
            network_access=network_access,
        )
        record, created = await asyncio.to_thread(self._create_source, source)
        return {
            "created": created,
            **await self._complete_source_registration(record, allow_onboarding=allow_onboarding),
        }

    async def register_source_url(
        self,
        *,
        source: str,
        network_access: str = "public",
        allow_onboarding: bool = True,
    ) -> dict[str, Any]:
        source_ref = source.strip()
        if not is_http_endpoint(source_ref):
            raise ValueError("External source must be an absolute HTTP(S) URL")
        if network_access not in {"public", "private"}:
            raise ValueError("network_access must be public or private")
        try:
            if network_access == "public":
                resolved, evidence, supported = await resolve_public_source(source_ref)
            else:
                resolved, evidence, supported = await resolve_private_toolhive_source(source_ref)
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError):
            logger.info("Source detection failed for %s", source_ref, exc_info=True)
            return {
                "source": source_ref,
                "probe_evidence": ["Source inspection failed or the endpoint is unavailable."],
                "unsupported_source": True,
            }
        if not supported or resolved is None:
            return {
                "source": source_ref,
                "probe_evidence": evidence,
                "unsupported_source": True,
            }
        record, created = await asyncio.to_thread(self._create_source, resolved)
        return {
            "created": created,
            "probe_evidence": evidence,
            **await self._complete_source_registration(record, allow_onboarding=allow_onboarding),
        }

    async def _complete_source_registration(
        self, record: SourceRecord, *, allow_onboarding: bool
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(_credential_challenge, record)
        provider = PROVIDERS[record.provider]
        if not result and provider.auto_onboard_single_result:
            if allow_onboarding:
                result = await self._onboard_single_source_result(record)
            else:
                result["onboarding"] = {
                    "status": "permission_required",
                    "message": "Adding this source's Thing requires Things write access.",
                }
        # Read after onboarding so the dependent count includes a newly created Thing.
        return {"source": await asyncio.to_thread(_management_source, record), **result}

    async def _onboard_single_source_result(self, record: SourceRecord) -> dict[str, Any]:
        try:
            source, public_http = await asyncio.to_thread(self._source_runtime, record)
            provider = PROVIDERS[source.provider]
            candidates = await provider.search(
                source,
                prepare_search_intent("", source),
                2,
                public_http=public_http,
            )
            if len(candidates) != 1:
                return {
                    "onboarding": {
                        "status": "selection_required" if candidates else "no_supported_things",
                        "message": (
                            "This source offers multiple Things. Choose which to add."
                            if candidates
                            else "This source has no supported Things to add."
                        ),
                    }
                }
            candidate = candidates[0]
            if (
                candidate.provider != record.provider
                or candidate.source_id != record.id
                or candidate.kind == "source"
                or "onboard" not in candidate.capabilities
            ):
                raise ValueError("Provider returned an invalid onboarding candidate")
            onboarding = await self._onboard_resource(
                source_record=record, source=source, candidate=candidate
            )
            return {"onboarding": onboarding}
        except CredentialChallengeError as exc:
            return {"credential_challenge": exc.public()}
        except SourceAuthenticationError:
            if record.security_scheme != "nosec":
                return {
                    "credential_challenge": {
                        "status": "credential_rejected",
                        "owner_kind": "source",
                        "source_id": record.id,
                        "security_name": record.security_name,
                        "scheme": record.security_scheme,
                    }
                }
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError, ValueError):
            logger.warning(
                "Automatic onboarding failed for source %s (provider %s)",
                record.id,
                record.provider,
                exc_info=True,
            )
        # Source registration has already succeeded. Keep it usable for a retry
        # and report the separate onboarding failure without upstream details.
        return {
            "onboarding": {
                "status": "onboarding_failed",
                "message": "The source was saved, but its Thing could not be added. Retry registration or discover it again.",
            }
        }

    async def update_registered_source(
        self,
        *,
        source_id: str,
        provider: str,
        title: str,
        description: str,
        tags: list[str],
        config: dict[str, Any],
        security: dict[str, Any] | None,
        network_access: str,
        allow_onboarding: bool = True,
    ) -> dict[str, Any]:
        current = await asyncio.to_thread(self._find_source, source_id)
        if current is None:
            raise ValueError("Discovery source was not found")
        replacement = _registered_source(
            provider=provider,
            title=title,
            description=description,
            tags=tags,
            config=config,
            security=security,
            network_access=network_access,
        )
        if (
            replacement.provider != current.provider
            or (replacement.external_id or replacement.id) != current.external_id
        ):
            raise ValueError(
                "Source provider and canonical identity cannot be changed; register a new source"
            )
        updated = await asyncio.to_thread(self._update_source_record, current, replacement)
        return await self._complete_source_registration(updated, allow_onboarding=allow_onboarding)

    async def delete_registered_source(self, *, source_id: str) -> None:
        def write() -> None:
            session_factory = get_session_factory()
            with session_factory() as session:
                if get_source(session, source_id) is None:
                    raise ValueError("Discovery source was not found")
                if count_source_dependents(session, source_id):
                    raise SourceConflictError(
                        "Discovery source cannot be deleted while Things depend on it"
                    )
                delete_source(session, source_id)
                session.commit()

        await asyncio.to_thread(write)

    async def preview_thing_refresh(
        self,
        *,
        thing_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        thing = await asyncio.to_thread(self._find_thing, thing_id)
        if (
            thing is None
            or thing.origin_kind != "discovery"
            or not thing.origin_provider
            or not thing.origin_source_id
        ):
            raise ValueError("Refresh is available only for provider-backed Things")
        source_record = await asyncio.to_thread(self._find_source, thing.origin_source_id)
        if source_record is None:
            raise SourceUnavailableError("The Thing's discovery source is unavailable")
        if source_record.provider != thing.origin_provider:
            raise RefreshConflictError("Thing and source providers no longer match")
        provider = PROVIDERS.get(thing.origin_provider)
        if provider is None or "refresh" not in provider.capabilities:
            raise ValueError("This discovery provider does not support refresh")
        try:
            source, _public_http = await asyncio.to_thread(self._source_runtime, source_record)
            generated = await provider.refresh_document(
                source,
                thing.document,
                external_id=str(thing.origin_external_id or ""),
                runtime=WotRuntimeClient(self._settings),
            )
            prepared, credentials_to_remove = provider.merge_refresh(
                thing.document,
                generated.document,
            )
            prepared = validate_document(prepared)
        except HTTPException as exc:
            raise ValueError(
                f"Provider generated an invalid Thing Description: {exc.detail}"
            ) from exc
        except SourceAuthenticationError as exc:
            raise CredentialChallengeError(
                status="credential_rejected",
                source_id=source_record.id,
                security_name=source_record.security_name,
                scheme=source_record.security_scheme,
                message="The external source rejected its stored credential.",
            ) from exc
        except CredentialChallengeError:
            raise
        except SourceConfigurationError:
            logger.warning(
                "Discovery source %s has an unusable stored configuration",
                source_record.id,
                exc_info=True,
            )
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError) as exc:
            logger.warning(
                "Refresh preview failed for Thing %s from source %s",
                thing_id,
                source_record.id,
                exc_info=True,
            )
            raise SourceUnavailableError(
                "The external source is unavailable or returned an invalid response"
            ) from exc

        diff = provider.refresh_diff(thing.document, prepared)
        refresh = RefreshRecord(
            user_id=user_id,
            thing_id=thing.id,
            source_id=source_record.id,
            provider=source_record.provider,
            thing_document_hash=thing.document_hash,
            source_hash=_source_record_hash(source_record),
            document=prepared,
            credentials_to_remove=credentials_to_remove,
            warnings=tuple(generated.warnings[:20]),
        )
        refresh_id = await self._refresh_store.put(refresh)
        return {
            "refresh_id": refresh_id,
            "expires_in_seconds": self._settings.discovery_refresh_ttl_seconds,
            "thing_id": thing.id,
            "diff": diff,
            "warnings": list(refresh.warnings),
        }

    async def apply_thing_refresh(
        self,
        *,
        thing_id: str,
        refresh_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            refresh = await self._refresh_store.get(refresh_id, user_id=user_id)
        except ValueError as exc:
            raise RefreshConflictError(str(exc)) from exc
        if refresh.thing_id != thing_id:
            raise RefreshConflictError("Refresh preview belongs to a different Thing")
        thing = await asyncio.to_thread(self._find_thing, thing_id)
        source = await asyncio.to_thread(self._find_source, refresh.source_id)
        if thing is None or source is None:
            raise RefreshConflictError("Thing or discovery source no longer exists")
        if (
            thing.document_hash != refresh.thing_document_hash
            or thing.origin_provider != refresh.provider
            or thing.origin_source_id != refresh.source_id
            or _source_record_hash(source) != refresh.source_hash
        ):
            raise RefreshConflictError(
                "Thing or discovery source changed after the refresh preview"
            )

        def write() -> ThingRecord:
            session_factory = get_session_factory()
            with session_factory() as session:
                current = get_thing(session, thing_id)
                current_source = get_source(session, refresh.source_id)
                if current is None or current.document_hash != refresh.thing_document_hash:
                    raise RefreshConflictError("Thing changed after the refresh preview")
                if (
                    current_source is None
                    or _source_record_hash(current_source) != refresh.source_hash
                ):
                    raise RefreshConflictError("Discovery source changed after the refresh preview")
                return ThingCatalogWriteService(session).update_discovered(
                    thing_id,
                    refresh.document,
                    remove_credentials=refresh.credentials_to_remove,
                )

        record = await asyncio.to_thread(write)
        await self._refresh_store.delete(refresh_id)
        return {
            "refreshed": True,
            "thing": _thing_summary(record),
            "warnings": list(refresh.warnings),
        }

    async def invoke_thing_action(
        self,
        *,
        thing_id: str,
        action: str,
        input_data: Any,
        uri_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        thing = await asyncio.to_thread(self._find_thing, thing_id)
        if (
            thing is None
            or thing.origin_kind != "discovery"
            or not thing.origin_provider
            or not thing.origin_external_id
            or not thing.origin_source_id
        ):
            raise ValueError("Provider-backed action requires a discovered Thing")
        target = _provider_action_target(
            thing.document,
            thing_id=thing.id,
            action=action,
            provider=thing.origin_provider,
        )
        provided_uri_variables = _validated_uri_variables(
            uri_variables,
            allowed=target["path_variables"] + target["query_variables"],
        )
        if target["operation"] == "download":
            if input_data is not None and input_data != {}:
                raise ValueError("Download actions do not accept input")
            if provided_uri_variables:
                raise ValueError("Download actions do not accept URI variables")
        else:
            _validate_provider_input(input_data)
        source_record = await asyncio.to_thread(self._find_source, thing.origin_source_id)
        if source_record is None:
            raise SourceUnavailableError("The resource's discovery source is unavailable")
        if source_record.provider != thing.origin_provider:
            raise ValueError("Resource provider does not match its discovery source")
        source, public_http = await asyncio.to_thread(self._source_runtime, source_record)
        provider = PROVIDERS.get(source.provider)
        if provider is None:
            raise SourceUnavailableError("The resource's discovery provider is unavailable")
        try:
            if target["operation"] == "download":
                download, provider_ttl = await provider.acquire(
                    source,
                    external_id=thing.origin_external_id,
                    title=thing.title,
                    resource_id=target["resource_id"],
                    public_http=public_http,
                )
            else:
                response = await provider.invoke_api(
                    source,
                    external_id=thing.origin_external_id,
                    method=target["method"],
                    path=target["path"],
                    path_variables=target["path_variables"],
                    query_variables=target["query_variables"],
                    uri_variables=provided_uri_variables,
                    input_data=input_data,
                    public_http=public_http,
                )
        except SourceAuthenticationError as exc:
            raise CredentialChallengeError(
                status="credential_rejected",
                source_id=source_record.id,
                security_name=source.security_name,
                scheme=source.security_scheme,
                message="The external source rejected its stored credential.",
            ) from exc
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderError) as exc:
            logger.warning(
                "Provider action '%s' failed for Thing %s", action, thing_id, exc_info=True
            )
            raise SourceUnavailableError(
                "The external source is unavailable or returned an invalid response"
            ) from exc
        if target["operation"] == "invoke":
            if len(response.body) > 512 * 1024:
                raise SourceUnavailableError("Provider action returned an oversized response")
            content_type = str(response.content_type or "application/octet-stream")[:200]
            if "\r" in content_type or "\n" in content_type:
                content_type = "application/octet-stream"
            return {
                "kind": "response",
                "content_type": content_type,
                "body_base64": base64.b64encode(response.body).decode("ascii"),
            }

        handle = await self._download_store.put(download, ttl_seconds=provider_ttl)
        result: dict[str, Any] = {
            "kind": "download",
            "title": target["title"],
            "download_url": f"/api/discovery/downloads/{handle}",
            "filename": download.filename,
            "media_type": download.content_type,
            "expires_in_seconds": min(
                provider_ttl or self._settings.discovery_download_ttl_seconds,
                self._settings.discovery_download_ttl_seconds,
            ),
        }
        if download.size_bytes is not None:
            result["size_bytes"] = download.size_bytes
        return result

    async def _onboard_resource(
        self,
        *,
        source_record: SourceRecord,
        source: SourceDefinition,
        candidate: CandidateDraft,
    ) -> dict[str, Any]:
        provider = PROVIDERS.get(candidate.provider)
        if provider is None:
            raise ValueError("Candidate provider is unavailable")
        existing = await asyncio.to_thread(
            self._find_existing,
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_id=source_record.id,
        )
        if existing is not None:
            current_generation = existing.document.get("wotbot:generation")
            current_digest = (
                str(current_generation.get("specificationDigest") or "")
                if isinstance(current_generation, dict)
                else ""
            )
            current_compiler_version = (
                current_generation.get("compilerVersion")
                if isinstance(current_generation, dict)
                else None
            )
            candidate_digest = str(candidate.payload.get("spec_digest") or "")
            candidate_compiler_version = candidate.payload.get("compiler_version")
            result: dict[str, Any] = {
                "created": False,
                "thing": _thing_summary(existing),
                "refresh_available": bool(
                    "refresh" in provider.capabilities
                    and (
                        (candidate_digest and current_digest and candidate_digest != current_digest)
                        or (
                            candidate_compiler_version is not None
                            and candidate_compiler_version != current_compiler_version
                        )
                    )
                ),
            }
            suggested_sources = provider.suggest_sources(candidate)
            if suggested_sources:
                result["suggested_sources"] = [dict(item) for item in suggested_sources[:5]]
            return result
        try:
            onboarding = await provider.onboarding_document(
                source,
                candidate,
                runtime=WotRuntimeClient(self._settings),
            )
            document = validate_document(onboarding.document)
        except HTTPException as exc:
            raise ValueError(
                f"Provider returned an invalid Thing Description: {exc.detail}"
            ) from exc

        def write() -> dict[str, Any]:
            session_factory = get_session_factory()
            with session_factory() as session:
                try:
                    record, created = ThingCatalogWriteService(session).create_discovered(
                        document,
                        provider=candidate.provider,
                        external_id=candidate.external_id,
                        source_id=source_record.id,
                    )
                except HTTPException as exc:
                    raise ValueError(str(exc.detail)) from exc
                result: dict[str, Any] = {
                    "created": created,
                    "thing": _thing_summary(record),
                    "warnings": list(onboarding.warnings[:20]),
                }
                if onboarding.suggested_sources:
                    result["suggested_sources"] = [
                        dict(item) for item in onboarding.suggested_sources[:5]
                    ]
                return result

        return await asyncio.to_thread(write)

    async def _store_many(
        self,
        drafts: list[CandidateDraft],
        *,
        thread_id: str,
        source_id: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        records: list[CandidateRecord] = []
        for draft in drafts:
            if draft.source_id != source_id or draft.provider != provider:
                raise ValueError("Provider returned a candidate for a different source")
            records.append(
                CandidateRecord.from_draft(draft, scope_kind="thread", scope_id=thread_id)
            )
        identifiers = await self._candidate_store.put_many(records)
        return [
            record.public(candidate_id)
            for record, candidate_id in zip(records, identifiers, strict=True)
        ]

    def _source_runtime(
        self, record: SourceRecord
    ) -> tuple[SourceDefinition, BoundedHttpClient | None]:
        try:
            source = _registered_source(
                provider=record.provider,
                title=record.title,
                description=record.description,
                tags=record.tags,
                config=record.config,
                security={"name": record.security_name, "scheme": record.security_scheme},
                network_access=record.network_access,
            )
        except ValueError as exc:
            # The stored record no longer satisfies its provider's config spec.
            raise SourceConfigurationError(str(exc)) from exc
        if (source.external_id or source.id) != record.external_id:
            raise SourceConfigurationError(
                "Source canonical identity does not match its stored identity"
            )
        source = replace(source, id=record.id, external_id=record.external_id)
        if source.security_scheme != "nosec":
            session_factory = get_session_factory()
            with session_factory() as session:
                credential = get_source_credential(
                    session,
                    source_id=record.id,
                    security_name=record.security_name,
                )
            if credential is None or credential.scheme != record.security_scheme:
                raise CredentialChallengeError(
                    status="credential_required",
                    source_id=record.id,
                    security_name=record.security_name,
                    scheme=record.security_scheme,
                    message="This external source requires credentials.",
                )
            source = replace(source, credential=dict(credential.credentials or {}))
        provider = PROVIDERS.get(source.provider)
        if provider is None:
            raise SourceConfigurationError(f"Unknown provider '{source.provider}'")
        return (
            source,
            BoundedHttpClient(
                mode="public",
                max_requests=provider.public_max_requests,
                max_bytes=provider.public_max_bytes,
            )
            if source.network_access == "public"
            else None,
        )

    @staticmethod
    def _create_source(source: SourceDefinition) -> tuple[SourceRecord, bool]:
        external_id = source.external_id or source.id
        record = SourceRecord(
            id=_source_id(source.provider, external_id),
            provider=source.provider,
            external_id=external_id,
            title=source.title,
            description=source.description,
            tags=list(source.tags),
            config=_source_config(source),
            network_access=source.network_access,
            security_name=source.security_name,
            security_scheme=source.security_scheme,
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            existing = get_source_by_identity(
                session, provider=record.provider, external_id=record.external_id
            )
            if existing is not None:
                return existing, False
            try:
                created = insert_source(session, record)
                session.commit()
                return created, True
            except IntegrityError:
                session.rollback()
                existing = get_source_by_identity(
                    session, provider=record.provider, external_id=record.external_id
                )
                if existing is not None:
                    return existing, False
                raise ValueError(
                    "Discovery source identity conflicts with an existing source"
                ) from None

    @staticmethod
    def _update_source_record(current: SourceRecord, replacement: SourceDefinition) -> SourceRecord:
        record = SourceRecord(
            id=current.id,
            provider=current.provider,
            external_id=current.external_id,
            title=replacement.title,
            description=replacement.description,
            tags=list(replacement.tags),
            config=_source_config(replacement),
            network_access=replacement.network_access,
            security_name=replacement.security_name,
            security_scheme=replacement.security_scheme,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            if (
                current.security_name != replacement.security_name
                or current.security_scheme != replacement.security_scheme
            ):
                delete_source_credentials(session, current.id)
            updated = update_source(session, record)
            session.commit()
            return updated

    @staticmethod
    def _find_existing(*, provider: str, external_id: str, source_id: str) -> ThingRecord | None:
        session_factory = get_session_factory()
        with session_factory() as session:
            return get_thing_by_origin(
                session,
                provider=provider,
                external_id=external_id,
                source_id=source_id,
            )

    @staticmethod
    def _find_thing(thing_id: str) -> ThingRecord | None:
        session_factory = get_session_factory()
        with session_factory() as session:
            return get_thing(session, thing_id)

    @staticmethod
    def _find_source(source_id: str) -> SourceRecord | None:
        session_factory = get_session_factory()
        with session_factory() as session:
            return get_source(session, source_id)

    @staticmethod
    def _list_source_records() -> list[SourceRecord]:
        session_factory = get_session_factory()
        with session_factory() as session:
            return list_sources(session)


def _credential_status(record: SourceRecord, stored_scheme: str | None) -> str:
    if record.security_scheme == "nosec":
        return "not_required"
    return "configured" if stored_scheme == record.security_scheme else "required"


def _public_source(record: SourceRecord, *, stored_scheme: str | None) -> dict[str, Any]:
    return {
        "source_id": record.id,
        "title": record.title,
        "description": record.description,
        "tags": record.tags,
        "provider": record.provider,
        "network_access": record.network_access,
        "credential_status": _credential_status(record, stored_scheme),
        "capabilities": list(
            getattr(PROVIDERS.get(record.provider), "capabilities", ("search", "onboard"))
        ),
    }


def _public_sources(records: list[SourceRecord]) -> list[dict[str, Any]]:
    """Serialize a page of sources using one session and one credential query."""

    session_factory = get_session_factory()
    with session_factory() as session:
        schemes = credential_schemes(session, [record.id for record in records])
    return [
        _public_source(record, stored_scheme=schemes.get((record.id, record.security_name)))
        for record in records
    ]


def _management_source(record: SourceRecord) -> dict[str, Any]:
    """Serialize one source for the management UI, in a single session."""

    session_factory = get_session_factory()
    with session_factory() as session:
        return _management_sources(session, [record])[0]


def _management_sources(session: Session, records: list[SourceRecord]) -> list[dict[str, Any]]:
    """Serialize a page for the management UI in two queries, not two per row."""

    identifiers = [record.id for record in records]
    schemes = credential_schemes(session, identifiers)
    dependents = dependent_counts(session, identifiers)
    return [
        {
            **_public_source(record, stored_scheme=schemes.get((record.id, record.security_name))),
            "external_id": record.external_id,
            "config": record.config,
            "security_name": record.security_name,
            "security_scheme": record.security_scheme,
            "dependent_thing_count": dependents.get(record.id, 0),
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }
        for record in records
    ]


def _source_id(provider: str, external_id: str) -> str:
    identity = f"{provider}\0source\0{external_id}".encode()
    return f"urn:wotbot:source:{provider}:{hashlib.sha256(identity).hexdigest()}"


def _source_config(source: SourceDefinition) -> dict[str, Any]:
    return {
        field: value
        for field, value in sorted(source.config.items())
        if value is not None and value != ""
    }


def _credential_challenge(record: SourceRecord) -> dict[str, Any]:
    if record.security_scheme == "nosec":
        return {}
    session_factory = get_session_factory()
    with session_factory() as session:
        credential = get_source_credential(
            session,
            source_id=record.id,
            security_name=record.security_name,
        )
    if credential is not None and credential.scheme == record.security_scheme:
        return {}
    return {
        "credential_challenge": {
            "status": "credential_required",
            "owner_kind": "source",
            "source_id": record.id,
            "security_name": record.security_name,
            "scheme": record.security_scheme,
        }
    }


def _source_misconfigured(source_id: str) -> dict[str, Any]:
    """Report a stored source that can no longer be rebuilt.

    Kept separate from ``_source_unavailable`` because the two need different
    fixes -- re-register the source, versus wait for the portal -- and because
    the underlying message can name internal configuration, so only this fixed
    text is ever returned.
    """

    return {
        "status": "source_misconfigured",
        "source_id": source_id,
        "items": [],
        "message": (
            "This discovery source's stored configuration is no longer valid. "
            "Re-register or edit the source; the external service was not contacted."
        ),
    }


def _source_unavailable(source_id: str, message: str) -> dict[str, Any]:
    return {
        "status": "source_unavailable",
        "source_id": source_id,
        "items": [],
        "message": message,
    }


def _registered_source(
    *,
    provider: str,
    title: str,
    description: str,
    tags: list[str],
    config: dict[str, Any],
    security: dict[str, Any] | None,
    network_access: str,
) -> SourceDefinition:
    implementation = PROVIDERS.get(provider)
    if implementation is None:
        raise ValueError(f"Unknown provider '{provider}'")
    if network_access not in {"public", "private"}:
        raise ValueError("network_access must be public or private")
    unknown_security = set(security or {}) - {"name", "scheme"}
    if unknown_security:
        raise ValueError("Unknown source security fields: " + ", ".join(sorted(unknown_security)))
    normalized = implementation.normalize_config(config)
    primary = implementation.external_identity(normalized)
    if not primary:
        raise ValueError("Provider configuration has no canonical identity")
    scheme = str((security or {}).get("scheme") or "").strip().lower()
    if not scheme:
        scheme = implementation.default_security_scheme
    if scheme not in {"nosec", "apikey", "bearer", "basic", "oauth2"}:
        raise ValueError("Unsupported source security scheme")
    security_name = str((security or {}).get("name") or "source_sc").strip()
    if not security_name or len(security_name) > 200:
        raise ValueError("Source security name must contain 1 to 200 characters")
    api_key_header = str(normalized.get("api_key_header") or "X-Api-Key")
    if scheme == "apikey" and not _HTTP_HEADER_NAME.fullmatch(api_key_header):
        raise ValueError("Source API key header is invalid")
    return SourceDefinition(
        id=primary,
        external_id=primary,
        provider=provider,
        title=(title.strip() or primary)[:500],
        description=description.strip()[:2000],
        tags=tuple(dict.fromkeys(str(tag)[:100] for tag in tags if str(tag).strip()))[:20],
        network_access=network_access,  # type: ignore[arg-type]
        config=normalized,
        security_name=security_name,
        security_scheme=scheme,
    )


def _validate_query(value: str) -> str:
    query = str(value or "").strip()
    if len(query) > 500:
        raise ValueError("Discover query must contain at most 500 characters")
    return query


def _validate_limit(value: int) -> int:
    if isinstance(value, bool):
        raise TypeError("Discover limit must be an integer")
    return max(1, min(int(value), 25))


def _candidate_draft(candidate: CandidateRecord) -> CandidateDraft:
    return CandidateDraft(
        provider=candidate.provider,
        source_id=candidate.source_id,
        external_id=candidate.external_id,
        kind=candidate.kind,
        title=candidate.title,
        summary=candidate.summary,
        links=candidate.links,
        payload=candidate.payload,
        capabilities=candidate.capabilities,
    )


def _provider_action_target(
    document: dict[str, Any],
    *,
    thing_id: str,
    action: str,
    provider: str,
) -> dict[str, Any]:
    actions = document.get("actions")
    affordance = actions.get(action) if isinstance(actions, dict) else None
    if not isinstance(affordance, dict):
        raise TypeError(f"Thing has no provider-backed action '{action}'")
    forms = affordance.get("forms")
    expected_href = provider_action_href(thing_id, action)
    form = (
        next(
            (
                item
                for item in forms
                if isinstance(item, dict)
                and (
                    item.get("href") == expected_href
                    or (
                        isinstance(item.get("href"), str)
                        and str(item["href"]).startswith(expected_href + "{?")
                        and str(item["href"]).endswith("}")
                    )
                )
            ),
            None,
        )
        if isinstance(forms, list)
        else None
    )
    if not isinstance(form, dict):
        raise TypeError(f"Action '{action}' is not an allowed provider operation")
    operation = form.get("wotbot:providerOperation")
    if operation not in {"download", "invoke"}:
        raise ValueError(f"Action '{action}' is not an allowed provider operation")
    raw_resource_id = form.get("wotbot:resourceId")
    if raw_resource_id is not None and (
        not isinstance(raw_resource_id, str) or not raw_resource_id.strip()
    ):
        raise ValueError(f"Action '{action}' has an invalid provider resource identity")
    if operation == "download":
        if form.get("href") != expected_href:
            raise ValueError(f"Action '{action}' has an invalid download target")
        return {
            "operation": "download",
            "resource_id": (raw_resource_id.strip() if isinstance(raw_resource_id, str) else None),
            "title": str(affordance.get("title") or action)[:500],
            "path_variables": (),
            "query_variables": (),
        }

    if provider not in {"edc-v3", "tx-bootstrap"}:
        raise ValueError(f"Provider '{provider}' does not expose proxied API actions")
    if affordance.get("wotbot:generatedBy") != provider:
        raise ValueError(f"Action '{action}' is not generated by its discovery provider")
    method = str(form.get("wotbot:httpMethod") or "").upper()
    path = form.get("wotbot:path")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}:
        raise ValueError(f"Action '{action}' has an invalid HTTP method")
    if (
        not isinstance(path, str)
        or len(path) > 2_000
        or not path.startswith("/")
        or any(value in path for value in ("?", "#", "\\"))
    ):
        raise ValueError(f"Action '{action}' has an invalid API path")
    path_variables = _provider_variable_names(form.get("wotbot:pathVariables"))
    query_variables = _provider_variable_names(form.get("wotbot:queryVariables"))
    if set(re.findall(r"\{([^{}]+)\}", path)) != set(path_variables):
        raise ValueError(f"Action '{action}' has inconsistent path variables")
    variables = (*path_variables, *query_variables)
    expected_template = "{?" + ",".join(variables) + "}" if variables else ""
    if form.get("href") != expected_href + expected_template:
        raise ValueError(f"Action '{action}' has an invalid provider target")
    return {
        "operation": "invoke",
        "title": str(affordance.get("title") or action)[:500],
        "method": method,
        "path": path,
        "path_variables": path_variables,
        "query_variables": query_variables,
    }


def _provider_variable_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Provider action has invalid URI-variable metadata")
    names = tuple(str(item) for item in value)
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name) for name in names
    ):
        raise ValueError("Provider action has invalid URI-variable metadata")
    return names


def _validated_uri_variables(
    value: dict[str, Any] | None,
    *,
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    variables = value or {}
    unknown = set(variables) - set(allowed)
    if unknown:
        raise ValueError("Provider action received undeclared URI variables")
    for name, item in variables.items():
        if item is None or not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"URI variable '{name}' must be a primitive value")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"URI variable '{name}' must be finite")
        if len(str(item)) > 4_000:
            raise ValueError(f"URI variable '{name}' is too long")
    return dict(variables)


def _validate_provider_input(value: Any) -> None:
    try:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Provider action input must be valid JSON") from exc
    if len(encoded) > 512 * 1024:
        raise ValueError("Provider action input is too large")


def _thing_summary(record: ThingRecord) -> dict[str, Any]:
    document = record.document if isinstance(record.document, dict) else {}
    affordances: dict[str, dict[str, Any]] = {}
    for kind in ("properties", "actions", "events"):
        values = document.get(kind)
        names = list(values) if isinstance(values, dict) else []
        affordances[kind] = {
            "count": len(names),
            "names": names[:30],
            "truncated": len(names) > 30,
        }
    return {
        **serialize_thing(record, include_document=False),
        "affordances": affordances,
    }


def _source_record_hash(record: SourceRecord) -> str:
    payload = {
        "id": record.id,
        "provider": record.provider,
        "external_id": record.external_id,
        "title": record.title,
        "description": record.description,
        "tags": record.tags,
        "config": record.config,
        "network_access": record.network_access,
        "security_name": record.security_name,
        "security_scheme": record.security_scheme,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
