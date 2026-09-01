from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from wotbot.catalog.credentials.store import delete_credential, delete_credentials_for_thing
from wotbot.catalog.events import build_change_event, build_remove_event
from wotbot.catalog.events.outbox import enqueue_thing_event
from wotbot.catalog.models import ThingConflictError, ThingDocument, ThingRecord
from wotbot.catalog.presentation import serialize_thing
from wotbot.catalog.store import (
    create_thing,
    delete_thing,
    get_thing,
    get_thing_by_origin,
    list_things,
    put_thing,
)


class ThingCatalogQueryService:
    def __init__(self, session: Session):
        self._session = session

    def list_owned_things(
        self,
        *,
        query: str = "",
        page: int = 1,
        per_page: int = 25,
        origin_kind: str | None = None,
    ) -> dict[str, Any]:
        if origin_kind not in {None, "manual", "discovery"}:
            raise HTTPException(status_code=422, detail="origin_kind must be manual or discovery")
        items, total = list_things(
            self._session,
            query=query,
            page=page,
            per_page=per_page,
            origin_kind=origin_kind,
        )
        return {
            "items": [serialize_thing(item) for item in items],
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def get_owned_thing(self, thing_id: str) -> dict[str, Any]:
        record = self._get_thing_or_404(thing_id)
        return serialize_thing(record, include_document=True)

    def list_affordances(self, thing_id: str, kind: str) -> dict[str, Any]:
        record = self._get_thing_or_404(thing_id)
        return {
            "thing_id": thing_id,
            kind: self._extract_affordances(record, kind),
        }

    def get_affordance(self, thing_id: str, kind: str, name: str) -> dict[str, Any]:
        record = self._get_thing_or_404(thing_id)
        affordances = self._extract_affordances(record, kind)
        if name not in affordances:
            label = kind[:-1].capitalize()
            raise HTTPException(status_code=404, detail=f"{label} '{name}' not found")
        return {"name": name, "definition": affordances[name]}

    def _get_thing_or_404(self, thing_id: str) -> ThingRecord:
        record = get_thing(self._session, thing_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Thing not found")
        return record

    def _extract_affordances(self, record: ThingRecord, kind: str) -> dict[str, Any]:
        document = record.document if isinstance(record.document, dict) else {}
        return document.get(kind, {}) or {}


class ThingCatalogWriteService:
    def __init__(self, session: Session):
        self._session = session

    def create(self, document: ThingDocument) -> ThingRecord:
        if _contains_provider_binding(document):
            raise HTTPException(
                status_code=422,
                detail="Provider-backed Things must be created through discovery onboarding",
            )
        try:
            record = create_thing(self._session, document)
            enqueue_thing_event(
                self._session,
                build_change_event("create", record),
            )
            self._session.commit()
        except ThingConflictError as exc:
            self._session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            self._session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            self._session.rollback()
            raise

        return record

    def create_discovered(
        self,
        document: ThingDocument,
        *,
        provider: str,
        external_id: str,
        source_id: str,
    ) -> tuple[ThingRecord, bool]:
        existing = get_thing_by_origin(
            self._session,
            provider=provider,
            external_id=external_id,
            source_id=source_id,
        )
        if existing is not None:
            return existing, False
        try:
            record = create_thing(
                self._session,
                document,
                origin_kind="discovery",
                origin_provider=provider,
                origin_external_id=external_id,
                origin_source_id=source_id,
            )
            enqueue_thing_event(self._session, build_change_event("create", record))
            self._session.commit()
        except ThingConflictError as exc:
            self._session.rollback()
            existing = get_thing_by_origin(
                self._session,
                provider=provider,
                external_id=external_id,
                source_id=source_id,
            )
            if existing is not None:
                return existing, False
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            self._session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            self._session.rollback()
            raise
        return record, True

    def update(self, thing_id: str, document: ThingDocument) -> ThingRecord:
        current = get_thing(self._session, thing_id)
        if (current is None or current.origin_kind == "manual") and _contains_provider_binding(
            document
        ):
            raise HTTPException(
                status_code=422,
                detail="Provider-backed Things must be created through discovery onboarding",
            )
        if current is not None and current.origin_kind == "discovery":
            _validate_protected_resource_update(
                current.document,
                document,
                provider=current.origin_provider,
            )
        try:
            record, created = put_thing(
                self._session,
                thing_id,
                document,
            )
            enqueue_thing_event(
                self._session,
                build_change_event("create" if created else "update", record),
            )
            self._session.commit()
        except ValueError as exc:
            self._session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            self._session.rollback()
            raise

        return record

    def update_discovered(
        self,
        thing_id: str,
        document: ThingDocument,
        *,
        clear_credentials: bool = False,
        remove_credentials: tuple[str, ...] = (),
    ) -> ThingRecord:
        current = get_thing(self._session, thing_id)
        if current is None or current.origin_kind != "discovery":
            raise HTTPException(status_code=404, detail="Discovered Thing not found")
        try:
            record, created = put_thing(self._session, thing_id, document)
            if created:
                raise ValueError("Discovered Thing identity changed during update")
            if clear_credentials:
                delete_credentials_for_thing(self._session, thing_id)
            else:
                for security_name in remove_credentials:
                    delete_credential(self._session, thing_id, security_name)
            enqueue_thing_event(self._session, build_change_event("update", record))
            self._session.commit()
        except ValueError as exc:
            self._session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            self._session.rollback()
            raise
        return record

    def delete(self, thing_id: str) -> None:
        self._delete(thing_id)

    def _delete(self, thing_id: str) -> None:
        try:
            deleted = delete_thing(self._session, thing_id)
            if not deleted:
                self._session.rollback()
                raise HTTPException(status_code=404, detail="Thing not found")

            delete_credentials_for_thing(self._session, thing_id)

            enqueue_thing_event(self._session, build_remove_event(thing_id))
            self._session.commit()
        except HTTPException:
            raise
        except Exception:
            self._session.rollback()
            raise


def _contains_provider_binding(document: ThingDocument) -> bool:
    actions = document.get("actions")
    if not isinstance(actions, dict):
        return False
    return any(
        isinstance(form, dict) and str(form.get("href") or "").startswith("wotbot+provider:")
        for affordance in actions.values()
        if isinstance(affordance, dict)
        for form in affordance.get("forms", ())
        if isinstance(affordance.get("forms"), list)
    )


def _validate_protected_resource_update(
    current: ThingDocument,
    replacement: ThingDocument,
    *,
    provider: str | None,
) -> None:
    if replacement.get("id") != current.get("id"):
        raise HTTPException(status_code=409, detail="Provider-backed Thing identity is protected")
    if replacement.get("security") != current.get("security") or replacement.get(
        "securityDefinitions"
    ) != current.get("securityDefinitions"):
        raise HTTPException(
            status_code=409,
            detail="Provider-backed Thing security is protected",
        )
    current_forms = _interaction_forms(current)
    replacement_forms = _interaction_forms(replacement)
    if provider in {"openapi", "edc-v3", "tx-bootstrap"}:
        current_generated = _provider_generated_actions(current, provider)
        if _provider_generated_actions(replacement, provider) != current_generated:
            raise HTTPException(
                status_code=409,
                detail=(
                    "OpenAPI-generated actions are protected"
                    if provider == "openapi"
                    else "Provider-generated actions are protected"
                ),
            )
        generated_names = set(current_generated)
        current_forms = {
            "actions": {
                name: forms
                for name, forms in current_forms.get("actions", {}).items()
                if name in generated_names
            }
        }
        replacement_forms = {
            "actions": {
                name: forms
                for name, forms in replacement_forms.get("actions", {}).items()
                if name in generated_names
            }
        }
    if replacement_forms != current_forms:
        raise HTTPException(
            status_code=409,
            detail="Provider-backed Thing action forms are protected",
        )
    if current.get("wotbot:generation") != replacement.get("wotbot:generation"):
        raise HTTPException(
            status_code=409,
            detail="Provider-backed Thing generation metadata is protected",
        )
    if _generation_markers(current) != _generation_markers(replacement):
        raise HTTPException(
            status_code=409,
            detail="Provider-backed Thing generation markers are protected",
        )


def _interaction_forms(
    document: ThingDocument,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    protected: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for affordance_kind in ("properties", "actions", "events"):
        affordances = document.get(affordance_kind)
        if not isinstance(affordances, dict):
            continue
        forms_by_name: dict[str, list[dict[str, Any]]] = {}
        for affordance_name, affordance in affordances.items():
            if not isinstance(affordance, dict):
                continue
            forms = affordance.get("forms")
            if isinstance(forms, list):
                forms_by_name[str(affordance_name)] = [
                    form for form in forms if isinstance(form, dict)
                ]
        if forms_by_name:
            protected[affordance_kind] = forms_by_name
    return protected


def _provider_generated_actions(
    document: ThingDocument,
    provider: str,
) -> dict[str, dict[str, Any]]:
    actions = document.get("actions")
    return {
        str(name): affordance
        for name, affordance in (actions.items() if isinstance(actions, dict) else ())
        if isinstance(affordance, dict) and affordance.get("wotbot:generatedBy") == provider
    }


def _generation_markers(document: ThingDocument) -> dict[str, Any]:
    actions = document.get("actions")
    action_markers = {
        str(name): {
            "generated_by": affordance.get("wotbot:generatedBy"),
            "operation_key": affordance.get("wotbot:operationKey"),
        }
        for name, affordance in (actions.items() if isinstance(actions, dict) else ())
        if isinstance(affordance, dict) and affordance.get("wotbot:generatedBy")
    }
    links = document.get("links")
    generated_links = [
        link
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, dict) and link.get("wotbot:generatedBy")
    ]
    api_description = document.get("wotbot:apiDescription")
    generated_api_description = (
        api_description
        if isinstance(api_description, dict) and api_description.get("wotbot:generatedBy")
        else None
    )
    return {
        "actions": action_markers,
        "links": generated_links,
        "api_description": generated_api_description,
    }
