from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from langchain_core.messages import HumanMessage, SystemMessage

from wotbot.catalog.enrichment.config import EnrichmentConfig
from wotbot.catalog.enrichment.models import (
    AffordanceAnnotation,
    EnrichmentDiffItem,
    EnrichmentProposal,
    EnrichmentResult,
    EnrichmentValidation,
    ShaclFinding,
)
from wotbot.catalog.enrichment.shacl import validate_enriched_document
from wotbot.catalog.enrichment.vocab import (
    Vocabulary,
    build_cached_vocabulary,
    unknown_proposal_iris,
)
from wotbot.catalog.validation import validate_document


class EnrichmentError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        errors: list[str] | None = None,
        shacl_findings: list[ShaclFinding] | None = None,
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.shacl_findings = shacl_findings or []


async def enrich_thing_document(
    document: dict[str, Any],
    *,
    config: EnrichmentConfig,
    llm: Any,
    max_repair_attempts: int = 2,
    runnable_config: dict[str, Any] | None = None,
) -> EnrichmentResult:
    try:
        sanitized = validate_document(document)
    except HTTPException:
        raise
    vocabulary = build_cached_vocabulary(config.model_dump_json())
    structured_llm = llm.with_structured_output(EnrichmentProposal)

    errors: list[str] = []
    shacl_findings: list[ShaclFinding] = []
    max_attempts = max(1, max_repair_attempts + 1)
    for attempt in range(1, max_attempts + 1):
        proposal = await _invoke_proposal(
            structured_llm,
            document=sanitized,
            config=config,
            vocabulary=vocabulary,
            errors=errors,
            runnable_config=runnable_config,
        )
        shacl_findings = []
        unknown = unknown_proposal_iris(proposal, vocabulary)
        if unknown:
            errors = [f"Unknown semantic IRI: {iri}" for iri in unknown]
            continue

        enriched, diff = merge_enrichment(sanitized, proposal, vocabulary=vocabulary)
        shacl_conforms, shacl_findings = validate_enriched_document(
            enriched, shapes_path=config.shapes
        )
        blocking_findings = [finding for finding in shacl_findings if finding.blocks_enrichment]
        if not shacl_conforms or blocking_findings:
            errors = _shacl_repair_errors(blocking_findings or shacl_findings)
            continue

        try:
            validated = validate_document(enriched)
        except HTTPException as exc:
            errors = [f"Enriched TD failed validation: {exc.detail}"]
            continue

        return EnrichmentResult(
            enriched=validated,
            diff=diff,
            validation=EnrichmentValidation(
                ok=True,
                attempts=attempt,
                shacl_conforms=shacl_conforms,
                shacl_findings=shacl_findings,
            ),
        )

    raise EnrichmentError(
        "Unable to produce a valid enrichment proposal",
        errors=errors,
        shacl_findings=shacl_findings,
    )


def merge_enrichment(
    document: dict[str, Any],
    proposal: EnrichmentProposal,
    *,
    vocabulary: Vocabulary,
) -> tuple[dict[str, Any], list[EnrichmentDiffItem]]:
    enriched = deepcopy(document)
    diff: list[EnrichmentDiffItem] = []

    _ensure_context_prefixes(enriched, vocabulary=vocabulary, diff=diff)

    for iri in proposal.thing_types:
        _add_type(
            enriched,
            vocabulary.compact(vocabulary.expand(iri)),
            path="@type",
            diff=diff,
            label="Thing type",
            rationale=proposal.thing_rationale,
        )

    for annotation in proposal.affordances:
        _merge_affordance(enriched, annotation, vocabulary=vocabulary, diff=diff)

    _complete_numeric_property_units(enriched, vocabulary=vocabulary, diff=diff)

    return enriched, diff


async def _invoke_proposal(
    structured_llm: Any,
    *,
    document: dict[str, Any],
    config: EnrichmentConfig,
    vocabulary: Vocabulary,
    errors: list[str],
    runnable_config: dict[str, Any] | None = None,
) -> EnrichmentProposal:
    repair = ""
    if errors:
        repair = "\nRepair these validation errors from the previous proposal:\n" + "\n".join(
            f"- {error}" for error in errors
        )

    response = await structured_llm.ainvoke(
        [
            SystemMessage(content=_system_prompt(config, vocabulary)),
            HumanMessage(content=f"Thing Description JSON:\n{document!r}{repair}"),
        ],
        config=runnable_config,
    )
    return EnrichmentProposal.model_validate(response)


def _system_prompt(config: EnrichmentConfig, vocabulary: Vocabulary) -> str:
    unit_hints = ", ".join(
        f"{label} -> {unit}" for label, unit in vocabulary.measurement_unit_defaults()
    )
    unit_guidance = (
        f"When a matching unit is clear, set unit_iri; default units per "
        f"measurement: {unit_hints}. "
        if unit_hints
        else ""
    )
    return (
        "You enrich W3C Thing Descriptions with semantic annotations.\n"
        "Return only the structured annotation proposal. Do not rewrite the TD.\n"
        "Use only IRIs from the allowed vocabulary. Prefer full IRIs in the proposal.\n"
        "Suggest additive annotations only: Thing @type, affordance @type, and QUDT units "
        "for numeric properties. If you assign a measurement semantic type to a numeric "
        f"property, you must also set unit_iri when a matching unit is clear. {unit_guidance}"
        "Include short rationales for each annotation.\n"
        f"{config.thing_enrichment_system_prompt_extra}\n\n"
        f"Allowed vocabulary:\n{vocabulary.prompt_terms()}"
    )


def _ensure_context_prefixes(
    document: dict[str, Any],
    *,
    vocabulary: Vocabulary,
    diff: list[EnrichmentDiffItem],
) -> None:
    context = document.get("@context")
    if isinstance(context, list):
        context_items = context
    elif context is None:
        context_items = []
        document["@context"] = context_items
    else:
        context_items = [context]
        document["@context"] = context_items

    prefix_map = _find_context_prefix_map(context_items)
    if prefix_map is None:
        prefix_map = {}
        context_items.append(prefix_map)

    for prefix, namespace in vocabulary.prefixes.items():
        if prefix_map.get(prefix) == namespace:
            continue
        if prefix in prefix_map:
            continue
        prefix_map[prefix] = namespace
        diff.append(
            EnrichmentDiffItem(
                kind="prefix",
                path=f"@context.{prefix}",
                value=namespace,
                label=f"Prefix {prefix}",
            )
        )


def _find_context_prefix_map(context_items: list[Any]) -> dict[str, Any] | None:
    for item in context_items:
        if isinstance(item, dict):
            return item
    return None


def _merge_affordance(
    document: dict[str, Any],
    annotation: AffordanceAnnotation,
    *,
    vocabulary: Vocabulary,
    diff: list[EnrichmentDiffItem],
) -> None:
    section = document.get(annotation.section)
    if not isinstance(section, dict):
        return
    target = section.get(annotation.name)
    if not isinstance(target, dict):
        return

    for iri in annotation.types:
        _add_type(
            target,
            vocabulary.compact(vocabulary.expand(iri)),
            path=f"{annotation.section}.{annotation.name}.@type",
            diff=diff,
            label=f"{annotation.name} type",
            rationale=annotation.rationale,
        )

    # Honor a unit the model proposed explicitly; inferred units are filled in
    # uniformly by _complete_numeric_property_units after all affordances merge.
    unit_predicate = vocabulary.unit_predicate()
    if (
        annotation.section == "properties"
        and annotation.unit_iri
        and unit_predicate
        and _needs_unit_completion(target)
        and unit_predicate not in target
    ):
        value = {"@id": vocabulary.compact(vocabulary.expand(annotation.unit_iri))}
        target[unit_predicate] = value
        diff.append(
            EnrichmentDiffItem(
                kind="unit",
                path=f"properties.{annotation.name}.{unit_predicate}",
                value=value,
                label=f"{annotation.name} unit",
                rationale=annotation.rationale,
            )
        )


def _complete_numeric_property_units(
    document: dict[str, Any],
    *,
    vocabulary: Vocabulary,
    diff: list[EnrichmentDiffItem],
) -> None:
    properties = document.get("properties")
    unit_predicate = vocabulary.unit_predicate()
    if not isinstance(properties, dict) or not unit_predicate:
        return

    for name, schema in properties.items():
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        if unit_predicate in schema or not _needs_unit_completion(schema):
            continue
        unit_iri = _infer_unit_iri_from_schema(schema, vocabulary)
        if not unit_iri:
            continue
        value = {"@id": vocabulary.compact(vocabulary.expand(unit_iri))}
        schema[unit_predicate] = value
        diff.append(
            EnrichmentDiffItem(
                kind="unit",
                path=f"properties.{name}.{unit_predicate}",
                value=value,
                label=f"{name} unit",
                rationale=_unit_inference_rationale(name, schema),
            )
        )


def _add_type(
    target: dict[str, Any],
    value: str,
    *,
    path: str,
    diff: list[EnrichmentDiffItem],
    label: str,
    rationale: str = "",
) -> None:
    current = target.get("@type")
    if current is None:
        target["@type"] = value
    elif isinstance(current, list):
        if value in current:
            return
        current.append(value)
    elif current == value:
        return
    else:
        target["@type"] = [current, value]

    diff.append(
        EnrichmentDiffItem(
            kind="type",
            path=path,
            value=value,
            label=label,
            rationale=rationale,
        )
    )


def _is_numeric_schema(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    if schema_type in {"number", "integer"}:
        return True
    return isinstance(schema_type, list) and bool({"number", "integer"} & set(schema_type))


def _needs_unit_completion(schema: dict[str, Any]) -> bool:
    """A property needs a QUDT unit if it is numeric or carries a semantic @type.

    Many TDs omit an explicit JSON Schema ``type`` on their properties. When a
    measurement semantic type (e.g. ``saref:Temperature``) is attached, the SHACL
    shapes still require a ``qudt:unit``, so unit completion must not be gated on
    the property declaring an explicit numeric type.
    """

    return _is_numeric_schema(schema) or _has_semantic_type(schema)


def _has_semantic_type(schema: dict[str, Any]) -> bool:
    current = schema.get("@type")
    if isinstance(current, str):
        return bool(current.strip())
    if isinstance(current, list):
        return any(isinstance(value, str) and value.strip() for value in current)
    return False


def _infer_unit_iri_from_schema(
    schema: dict[str, Any],
    vocabulary: Vocabulary,
) -> str | None:
    from_existing = _unit_iri_from_existing_unit(schema, vocabulary)
    if from_existing:
        return from_existing

    # A measurement class the SHACL shape requires a unit for declares a default
    # unit in the vocabulary, so its annotation always resolves to a unit here.
    for semantic_iri in _schema_semantic_iris(schema):
        default_unit = vocabulary.default_unit_iri(semantic_iri)
        if default_unit:
            return default_unit

    return None


def _schema_semantic_iris(schema: dict[str, Any]) -> list[str]:
    current = schema.get("@type")
    if isinstance(current, str):
        return [current]
    if isinstance(current, list):
        return [value for value in current if isinstance(value, str)]
    return []


def _unit_iri_from_existing_unit(schema: dict[str, Any], vocabulary: Vocabulary) -> str | None:
    unit = schema.get("unit")
    if not isinstance(unit, str):
        return None
    return vocabulary.unit_iri_for_label(unit)


def _unit_inference_rationale(name: str, schema: dict[str, Any]) -> str:
    unit = schema.get("unit")
    if isinstance(unit, str) and unit.strip():
        return f"Matched existing TD unit {unit!r} to a configured QUDT unit."
    return f"Inferred a default QUDT unit from semantic type/name for {name!r}."


def _shacl_repair_errors(findings: list[ShaclFinding]) -> list[str]:
    return [
        (
            "SHACL "
            f"{finding.severity.removeprefix('http://www.w3.org/ns/shacl#')}: "
            f"{finding.message}"
            f" affordance={finding.focus_label or 'unknown'}"
            f" focus={finding.focus_node or 'unknown'}"
            f" path={finding.result_path or 'unknown'}"
        )
        for finding in findings
    ]
