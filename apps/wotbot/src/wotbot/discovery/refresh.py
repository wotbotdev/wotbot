"""Merging a regenerated Thing Description over the one already stored.

A provider-generated Thing may also carry affordances a person added by hand.
Regenerating must replace everything the provider owns and preserve everything
it does not, so each generated node is stamped with ``wotbot:generatedBy`` and
this module partitions on that stamp.

The rules here are provider-neutral: the marker is passed in. Providers reach
them through :meth:`DiscoveryProvider.merge_refresh`, so orchestration never
needs to know which provider generated a document.
"""

from __future__ import annotations

from typing import Any

from wotbot.discovery.errors import RefreshConflictError

GENERATED_BY = "wotbot:generatedBy"


def is_generated_by(value: Any, marker: str) -> bool:
    """Return whether one TD node was produced by the named provider."""

    return isinstance(value, dict) and value.get(GENERATED_BY) == marker


def _partition(values: Any, marker: str) -> tuple[dict[str, Any], set[str]]:
    """Split a TD affordance map into its manual entries and generated names."""

    entries = values if isinstance(values, dict) else {}
    manual = {
        str(name): value for name, value in entries.items() if not is_generated_by(value, marker)
    }
    generated = {str(name) for name, value in entries.items() if is_generated_by(value, marker)}
    return manual, generated


def merge_generated_document(
    current: dict[str, Any],
    generated: dict[str, Any],
    *,
    marker: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Overlay a freshly generated document on the stored one.

    Returns the merged document and the names of security definitions whose
    stored credentials no longer apply and must be dropped.
    """

    replacement = dict(generated)
    replacement["id"] = current.get("id")
    for field in ("title", "description"):
        if field in current:
            replacement[field] = current[field]

    # Properties and events are never generated, so the stored ones stand.
    for kind in ("properties", "events"):
        values = current.get(kind)
        if isinstance(values, dict) and values:
            replacement[kind] = dict(values)

    manual_actions, _ = _partition(current.get("actions"), marker)
    new_actions = generated.get("actions")
    new_actions = dict(new_actions) if isinstance(new_actions, dict) else {}
    collisions = set(manual_actions) & set(new_actions)
    if collisions:
        raise RefreshConflictError(
            "Generated actions conflict with manually added actions: "
            + ", ".join(sorted(collisions)[:10])
        )
    replacement["actions"] = {**manual_actions, **new_actions}

    current_definitions = current.get("securityDefinitions")
    current_definitions = dict(current_definitions) if isinstance(current_definitions, dict) else {}
    generated_definitions = generated.get("securityDefinitions")
    generated_definitions = (
        dict(generated_definitions) if isinstance(generated_definitions, dict) else {}
    )
    manual_definitions, old_generated_names = _partition(current_definitions, marker)
    definition_collisions = set(manual_definitions) & set(generated_definitions)
    if definition_collisions:
        raise RefreshConflictError(
            "Generated security definitions conflict with manual definitions: "
            + ", ".join(sorted(definition_collisions)[:10])
        )
    replacement["securityDefinitions"] = {**manual_definitions, **generated_definitions}

    # A credential is only still valid if its definition survived unchanged.
    credentials_to_remove = {
        name
        for name in old_generated_names
        if name not in generated_definitions
        or current_definitions.get(name) != generated_definitions.get(name)
    }

    current_security = current.get("security")
    current_security = current_security if isinstance(current_security, list) else []
    manual_security = [
        str(name) for name in current_security if str(name) not in old_generated_names
    ]
    generated_security = generated.get("security")
    replacement["security"] = list(
        dict.fromkeys(
            [
                *manual_security,
                *(
                    [str(name) for name in generated_security]
                    if isinstance(generated_security, list)
                    else []
                ),
            ]
        )
    )

    current_links = current.get("links")
    current_links = current_links if isinstance(current_links, list) else []
    manual_links = [value for value in current_links if not is_generated_by(value, marker)]
    generated_links = generated.get("links")
    replacement["links"] = [
        *manual_links,
        *(generated_links if isinstance(generated_links, list) else []),
    ]
    return replacement, tuple(sorted(credentials_to_remove))


def generated_diff(
    current: dict[str, Any],
    replacement: dict[str, Any],
    *,
    marker: str,
) -> dict[str, Any]:
    """Summarize what regenerating would change, for review before applying."""

    before = current.get("actions")
    after = replacement.get("actions")
    before_generated = {
        name: value
        for name, value in (before.items() if isinstance(before, dict) else ())
        if is_generated_by(value, marker)
    }
    after_generated = {
        name: value
        for name, value in (after.items() if isinstance(after, dict) else ())
        if is_generated_by(value, marker)
    }
    changed = [
        name
        for name in sorted(set(before_generated) & set(after_generated))
        if before_generated[name] != after_generated[name]
    ]
    before_generation = current.get("wotbot:generation")
    after_generation = replacement.get("wotbot:generation")
    before_digest = (
        before_generation.get("specificationDigest")
        if isinstance(before_generation, dict)
        else None
    )
    after_digest = (
        after_generation.get("specificationDigest") if isinstance(after_generation, dict) else None
    )
    return {
        "added_actions": sorted(set(after_generated) - set(before_generated))[:30],
        "removed_actions": sorted(set(before_generated) - set(after_generated))[:30],
        "changed_actions": changed[:30],
        "metadata_changed": (
            current.get("wotbot:apiDescription") != replacement.get("wotbot:apiDescription")
            or bool(before_digest and after_digest and before_digest != after_digest)
        ),
        "server_changed": (
            (before_generation.get("serverUrl") if isinstance(before_generation, dict) else None)
            != (after_generation.get("serverUrl") if isinstance(after_generation, dict) else None)
        ),
        "security_changed": current.get("securityDefinitions")
        != replacement.get("securityDefinitions"),
    }
