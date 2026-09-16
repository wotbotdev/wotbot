from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Graph

from wotbot.catalog.enrichment.config import load_enrichment_config
from wotbot.catalog.enrichment.models import EnrichmentProposal
from wotbot.catalog.enrichment.service import (
    EnrichmentError,
    enrich_thing_document,
    merge_enrichment,
)
from wotbot.catalog.enrichment.shacl import validate_enriched_document
from wotbot.catalog.enrichment.vocab import build_vocabulary, unknown_proposal_iris

# The packaged core is vocabulary-agnostic (SOSA + QUDT only). Tests that exercise
# domain-vocabulary enrichment load a synthetic SAREF-shaped profile from
# tests/fixtures/enrichment (namespaces under http://wotbot.test/...), keeping the
# real SAREF stack out of the shipped package. Overlay/merge tests deliberately
# use the packaged core via load_enrichment_config() with no argument.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "enrichment"
FIXTURE_CONFIG = str(FIXTURE_DIR / "enrichment.json")
FIXTURE_SHAPES = str(FIXTURE_DIR / "shapes.ttl")


def _unit_required_measurement_classes() -> list[str]:
    vocab = build_vocabulary(load_enrichment_config(FIXTURE_CONFIG))
    return sorted(vocab.measurement_classes_requiring_unit())


def sample_thing() -> dict[str, object]:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thing:alpha",
        "title": "Alpha temperature sensor",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        "properties": {
            "temperature": {
                "type": "number",
                "unit": "°C",
                "readOnly": True,
                "forms": [{"href": "https://example.test/temp"}],
            }
        },
    }


def sample_thing_without_unit() -> dict[str, object]:
    document = sample_thing()
    document["properties"]["temperature"].pop("unit", None)
    return document


def sample_environment_thing_without_units() -> dict[str, object]:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thing:environment",
        "title": "Environment sensor",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        "properties": {
            "temperature": {"type": "number", "forms": [{"href": "https://example.test/temp"}]},
            "humidity": {"type": "number", "forms": [{"href": "https://example.test/humidity"}]},
            "light": {"type": "number", "forms": [{"href": "https://example.test/light"}]},
        },
    }


def sample_untyped_environment_thing() -> dict[str, object]:
    """Environment sensor whose properties omit an explicit JSON Schema type."""
    document = sample_environment_thing_without_units()
    for schema in document["properties"].values():
        schema.pop("type", None)
    return document


def sample_semantic_environment_thing_without_units() -> dict[str, object]:
    document = sample_environment_thing_without_units()
    document["@context"] = [
        document["@context"],
        {
            "dev": "http://wotbot.test/dev/",
            "qudt": "http://qudt.org/schema/qudt/",
            "unit": "http://qudt.org/vocab/unit/",
        },
    ]
    document["properties"]["temperature"]["@type"] = "dev:Temperature"
    document["properties"]["humidity"]["@type"] = "dev:Humidity"
    document["properties"]["light"]["@type"] = "dev:Illuminance"
    return document


def enriched_sample_thing() -> dict[str, object]:
    document = sample_thing()
    document["@context"] = [
        document["@context"],
        {
            "dev": "http://wotbot.test/dev/",
            "ts": "http://wotbot.test/ts/",
            "qudt": "http://qudt.org/schema/qudt/",
            "unit": "http://qudt.org/vocab/unit/",
        },
    ]
    document["@type"] = "dev:TemperatureSensor"
    document["properties"]["temperature"]["@type"] = "dev:Temperature"
    document["properties"]["temperature"]["qudt:unit"] = {"@id": "unit:DEG_C"}
    return document


class FakeStructuredLlm:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0
        self.configs = []

    async def ainvoke(self, _messages, config=None):
        self.calls += 1
        self.configs.append(config)
        return self.proposals.pop(0)


class FakeLlm:
    def __init__(self, proposals):
        self.structured = FakeStructuredLlm(proposals)

    def with_structured_output(self, _schema):
        return self.structured


def test_vocabulary_config_loads_packaged_terms_and_rejects_unknown_iris():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)

    assert vocab.contains("dev:TemperatureSensor")
    assert vocab.contains("ts:TimeSeriesMeasurement")
    assert vocab.contains("http://wotbot.test/env/FrequencyMeasurement")
    assert vocab.contains("http://qudt.org/vocab/unit/DEG_C")
    assert vocab.contains("unit:HZ")
    assert vocab.unit_iri_for_label("°C") == "http://qudt.org/vocab/unit/DEG_C"
    assert vocab.unit_iri_for_label("Hz") == "http://qudt.org/vocab/unit/HZ"

    proposal = EnrichmentProposal(thing_types=["http://wotbot.test/dev/DefinitelyNotARealTerm"])
    assert unknown_proposal_iris(proposal, vocab) == [
        "http://wotbot.test/dev/DefinitelyNotARealTerm"
    ]


def test_merge_adds_context_types_units_and_preserves_existing_annotations():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)
    document = sample_thing()
    document["@type"] = "dev:Device"

    proposal = EnrichmentProposal(
        thing_rationale="Title says this is a temperature sensor.",
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
                "unit_iri": "http://qudt.org/vocab/unit/DEG_C",
                "rationale": "The property is numeric and named temperature.",
            }
        ],
    )

    enriched, diff = merge_enrichment(document, proposal, vocabulary=vocab)

    assert enriched["@type"] == ["dev:Device", "dev:TemperatureSensor"]
    temperature = enriched["properties"]["temperature"]
    assert temperature["@type"] == "dev:Temperature"
    assert temperature["qudt:unit"] == {"@id": "unit:DEG_C"}
    assert {item.kind for item in diff} == {"prefix", "type", "unit"}
    assert any(item.rationale for item in diff if item.kind == "type")

    enriched_again, diff_again = merge_enrichment(enriched, proposal, vocabulary=vocab)
    assert enriched_again == enriched
    assert [item for item in diff_again if item.kind != "prefix"] == []


def test_merge_infers_qudt_unit_from_existing_td_unit():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)
    proposal = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
            }
        ],
    )

    enriched, diff = merge_enrichment(sample_thing(), proposal, vocabulary=vocab)

    assert enriched["properties"]["temperature"]["qudt:unit"] == {"@id": "unit:DEG_C"}
    unit_diff = next(item for item in diff if item.kind == "unit")
    assert "Matched existing TD unit" in unit_diff.rationale


def test_merge_infers_default_units_from_semantic_types_and_names():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)
    proposal = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/Sensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
            },
            {
                "section": "properties",
                "name": "humidity",
                "types": ["http://wotbot.test/dev/Humidity"],
            },
            {
                "section": "properties",
                "name": "light",
                "types": ["http://wotbot.test/dev/Illuminance"],
            },
        ],
    )

    enriched, diff = merge_enrichment(
        sample_environment_thing_without_units(),
        proposal,
        vocabulary=vocab,
    )

    properties = enriched["properties"]
    assert properties["temperature"]["qudt:unit"] == {"@id": "unit:DEG_C"}
    assert properties["humidity"]["qudt:unit"] == {"@id": "unit:PERCENT"}
    assert properties["light"]["qudt:unit"] == {"@id": "unit:LUX"}
    assert sum(1 for item in diff if item.kind == "unit") == 3
    conforms, findings = validate_enriched_document(enriched, shapes_path=FIXTURE_SHAPES)
    assert conforms is True
    assert not [finding for finding in findings if finding.blocks_enrichment]


def test_merge_completes_units_for_measurement_types_without_numeric_schema():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)
    proposal = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/Sensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
            },
            {
                "section": "properties",
                "name": "humidity",
                "types": ["http://wotbot.test/dev/Humidity"],
            },
            {
                "section": "properties",
                "name": "light",
                "types": ["http://wotbot.test/dev/Illuminance"],
            },
        ],
    )

    enriched, _diff = merge_enrichment(
        sample_untyped_environment_thing(),
        proposal,
        vocabulary=vocab,
    )

    properties = enriched["properties"]
    assert properties["temperature"]["qudt:unit"] == {"@id": "unit:DEG_C"}
    assert properties["humidity"]["qudt:unit"] == {"@id": "unit:PERCENT"}
    assert properties["light"]["qudt:unit"] == {"@id": "unit:LUX"}
    conforms, findings = validate_enriched_document(enriched, shapes_path=FIXTURE_SHAPES)
    assert conforms is True
    assert not [finding for finding in findings if finding.blocks_enrichment]


@pytest.mark.parametrize("measurement_iri", _unit_required_measurement_classes())
def test_every_unit_required_measurement_class_resolves_and_conforms(measurement_iri):
    # Every measurement class that declares a default unit must be inferable with
    # no numeric schema or unit hint, so it can always satisfy the SHACL shape.
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)
    document = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thing:measurement",
        "title": "Bare measurement",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        "properties": {
            "reading": {
                "@type": measurement_iri,
                "forms": [{"href": "https://example.test/reading"}],
            }
        },
    }

    enriched, _diff = merge_enrichment(document, EnrichmentProposal(), vocabulary=vocab)

    expected = vocab.compact(vocab.default_unit_iri(measurement_iri))
    assert enriched["properties"]["reading"]["qudt:unit"] == {"@id": expected}
    conforms, findings = validate_enriched_document(enriched, shapes_path=FIXTURE_SHAPES)
    assert conforms is True
    assert not [finding for finding in findings if finding.blocks_enrichment]


def test_merge_completes_units_for_preexisting_semantic_properties():
    config = load_enrichment_config(FIXTURE_CONFIG)
    vocab = build_vocabulary(config)

    enriched, diff = merge_enrichment(
        sample_semantic_environment_thing_without_units(),
        EnrichmentProposal(),
        vocabulary=vocab,
    )

    properties = enriched["properties"]
    assert properties["temperature"]["qudt:unit"] == {"@id": "unit:DEG_C"}
    assert properties["humidity"]["qudt:unit"] == {"@id": "unit:PERCENT"}
    assert properties["light"]["qudt:unit"] == {"@id": "unit:LUX"}
    assert sum(1 for item in diff if item.kind == "unit") == 3
    conforms, findings = validate_enriched_document(enriched, shapes_path=FIXTURE_SHAPES)
    assert conforms is True
    assert not [finding for finding in findings if finding.blocks_enrichment]


def test_shapes_require_units_match_vocab_defaults():
    # Drift guard: a domain shape's required-unit target classes must equal the
    # measurement classes the vocabulary can resolve a default unit for. If they
    # diverge, enrichment can produce a semantic type the shape rejects but can't
    # repair. Exercised against the synthetic fixture profile, which mirrors the
    # SAREF stack the demo ships.
    from wotbot.catalog.enrichment.shacl import _shapes_text

    graph = Graph()
    graph.parse(data=_shapes_text(FIXTURE_SHAPES), format="turtle")
    query = """
        PREFIX sh: <http://www.w3.org/ns/shacl#>
        SELECT ?cls WHERE {
            ?shape sh:targetClass ?cls ;
                   sh:property ?p .
            ?p sh:path <http://qudt.org/schema/qudt/unit> ;
               sh:minCount ?min .
        }
    """
    required = {str(row.cls) for row in graph.query(query)}
    vocab = build_vocabulary(load_enrichment_config(FIXTURE_CONFIG))
    assert required == set(vocab.measurement_classes_requiring_unit())


def test_external_shapes_path_is_resolved_relative_to_config(tmp_path):
    (tmp_path / "custom-shapes.ttl").write_text("# custom\n", encoding="utf-8")
    config_file = tmp_path / "enrichment.json"
    config_file.write_text(
        json.dumps(
            {
                "ontologies": [
                    {"prefix": "ex", "namespace": "http://example/", "terms": "ex.json"}
                ],
                "shapes": "custom-shapes.ttl",
            }
        ),
        encoding="utf-8",
    )

    config = load_enrichment_config(str(config_file))

    assert config.shapes == str(tmp_path / "custom-shapes.ttl")


def test_overlay_inherits_packaged_ontologies_when_override_omits_them(tmp_path):
    packaged = load_enrichment_config()
    config_file = tmp_path / "enrichment.json"
    config_file.write_text(
        json.dumps({"thing_enrichment_system_prompt_extra": "custom demo bias"}),
        encoding="utf-8",
    )

    config = load_enrichment_config(str(config_file))

    # The override wins for the key it provides...
    assert config.thing_enrichment_system_prompt_extra == "custom demo bias"
    # ...and inherits the packaged ontology stack for the keys it omits.
    assert [o.prefix for o in config.ontologies] == [o.prefix for o in packaged.ontologies]
    assert config.shapes == packaged.shapes
    # Inherited ``terms`` still resolve as packaged resources (production builds
    # the vocabulary without a config_path), so the vocabulary is non-empty.
    vocab = build_vocabulary(config)
    assert len(vocab.terms) == len(build_vocabulary(packaged).terms) > 0


def test_overlay_ontologies_merge_onto_packaged_by_prefix(tmp_path):
    config_file = tmp_path / "enrichment.json"
    config_file.write_text(
        json.dumps(
            {"ontologies": [{"prefix": "ex", "namespace": "http://example/", "terms": "ex.json"}]}
        ),
        encoding="utf-8",
    )

    packaged = load_enrichment_config()
    config = load_enrichment_config(str(config_file))

    # A new prefix is appended onto the packaged core, which is preserved...
    assert [o.prefix for o in config.ontologies] == [o.prefix for o in packaged.ontologies] + ["ex"]
    # ...the override entry's terms are absolutised relative to the config file...
    ex = next(o for o in config.ontologies if o.prefix == "ex")
    assert ex.terms == str(tmp_path / "ex.json")
    # ...and inherited core ontologies keep their packaged (relative) terms.
    first_core = packaged.ontologies[0]
    merged_core = next(o for o in config.ontologies if o.prefix == first_core.prefix)
    assert merged_core.terms == first_core.terms
    # The prompt extension was omitted -> inherited from the packaged default.
    assert (
        config.thing_enrichment_system_prompt_extra == packaged.thing_enrichment_system_prompt_extra
    )


def test_overlay_ontology_with_existing_prefix_overrides_core(tmp_path):
    packaged = load_enrichment_config()
    core_prefix = packaged.ontologies[0].prefix
    config_file = tmp_path / "enrichment.json"
    config_file.write_text(
        json.dumps(
            {
                "ontologies": [
                    {"prefix": core_prefix, "namespace": "http://override/", "terms": "c.json"}
                ]
            }
        ),
        encoding="utf-8",
    )

    config = load_enrichment_config(str(config_file))

    # Same prefix -> replaced in place, nothing appended.
    assert [o.prefix for o in config.ontologies] == [o.prefix for o in packaged.ontologies]
    overridden = next(o for o in config.ontologies if o.prefix == core_prefix)
    assert overridden.namespace == "http://override/"
    assert overridden.terms == str(tmp_path / "c.json")


def test_overlay_rejects_non_object_config(tmp_path):
    config_file = tmp_path / "enrichment.json"
    config_file.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(ValueError):
        load_enrichment_config(str(config_file))


def test_external_shapes_override_validation(tmp_path):
    shapes = tmp_path / "shapes.ttl"
    shapes.write_text(
        """
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix td: <https://www.w3.org/2019/wot/td#> .

        <#RequiresDescriptionShape>
            a sh:NodeShape ;
            sh:targetSubjectsOf td:title ;
            sh:property [
                sh:path td:description ;
                sh:minCount 1 ;
                sh:severity sh:Violation ;
                sh:message "Things must carry a description." ;
            ] .
        """,
        encoding="utf-8",
    )

    document = sample_thing()  # has a title, no description

    conforms_default, _ = validate_enriched_document(document)
    conforms_custom, findings = validate_enriched_document(document, shapes_path=str(shapes))

    assert conforms_default is True
    assert conforms_custom is False
    assert any("description" in finding.message for finding in findings)


def test_shacl_accepts_complete_enriched_sample():
    conforms, findings = validate_enriched_document(
        enriched_sample_thing(), shapes_path=FIXTURE_SHAPES
    )

    assert conforms is True
    assert findings == []


def test_shacl_blocks_semantic_measurement_without_unit():
    document = enriched_sample_thing()
    del document["properties"]["temperature"]["qudt:unit"]

    conforms, findings = validate_enriched_document(document, shapes_path=FIXTURE_SHAPES)

    assert conforms is False
    assert any("qudt:unit" in finding.message for finding in findings)
    assert any(finding.blocks_enrichment for finding in findings)
    assert any(finding.focus_label == "temperature" for finding in findings)


def test_shacl_blocks_timeseries_annotation_on_scalar_property():
    document = enriched_sample_thing()
    document["properties"]["temperature"]["@type"] = "ts:TimeSeriesMeasurement"
    del document["properties"]["temperature"]["qudt:unit"]

    conforms, findings = validate_enriched_document(document, shapes_path=FIXTURE_SHAPES)

    assert conforms is False
    assert any("Time-series annotations" in finding.message for finding in findings)


def test_shacl_warning_is_advisory():
    document = sample_thing()

    conforms, findings = validate_enriched_document(document, shapes_path=FIXTURE_SHAPES)

    assert conforms is True
    assert any(finding.severity.endswith("Warning") for finding in findings)
    assert not any(finding.blocks_enrichment for finding in findings)


@pytest.mark.anyio
async def test_enrich_repairs_unknown_iri_and_returns_valid_diff():
    config = load_enrichment_config(FIXTURE_CONFIG)
    bad = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TempSensor"],
    )
    good = EnrichmentProposal(
        thing_rationale="Title says this is a temperature sensor.",
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
                "unit_iri": "http://qudt.org/vocab/unit/DEG_C",
                "rationale": "The property is numeric and named temperature.",
            }
        ],
    )
    llm = FakeLlm([bad, good])

    result = await enrich_thing_document(
        sample_thing(),
        config=config,
        llm=llm,
        max_repair_attempts=1,
    )

    assert result.validation.ok is True
    assert result.validation.attempts == 2
    assert result.validation.shacl_conforms is True
    assert result.validation.shacl_findings == []
    assert llm.structured.calls == 2
    assert result.enriched["@type"] == "dev:TemperatureSensor"
    assert any(item.kind == "unit" for item in result.diff)
    assert any(item.rationale for item in result.diff)


@pytest.mark.anyio
async def test_enrich_infers_unit_before_shacl_repair_is_needed():
    config = load_enrichment_config(FIXTURE_CONFIG)
    missing_unit = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
                "rationale": "The property is numeric and named temperature.",
            }
        ],
    )
    llm = FakeLlm([missing_unit])

    result = await enrich_thing_document(
        sample_thing_without_unit(),
        config=config,
        llm=llm,
        max_repair_attempts=1,
    )

    assert result.validation.ok is True
    assert result.validation.attempts == 1
    assert llm.structured.calls == 1
    assert result.enriched["properties"]["temperature"]["qudt:unit"] == {"@id": "unit:DEG_C"}


@pytest.mark.anyio
async def test_enrich_forwards_runnable_config_to_llm():
    """runnable_config (e.g. stream-suppression tags) reaches every ainvoke."""
    config = load_enrichment_config(FIXTURE_CONFIG)
    proposal = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/dev/Temperature"],
                "rationale": "The property is numeric and named temperature.",
            }
        ],
    )
    llm = FakeLlm([proposal])
    runnable_config = {"tags": ["nostream"]}

    await enrich_thing_document(
        sample_thing_without_unit(),
        config=config,
        llm=llm,
        max_repair_attempts=1,
        runnable_config=runnable_config,
    )

    assert llm.structured.configs == [runnable_config]


@pytest.mark.anyio
async def test_enrich_fails_after_repair_budget():
    config = load_enrichment_config(FIXTURE_CONFIG)
    bad = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TempSensor"],
    )

    with pytest.raises(EnrichmentError) as exc_info:
        await enrich_thing_document(
            sample_thing(),
            config=config,
            llm=FakeLlm([bad]),
            max_repair_attempts=0,
        )

    assert "Unknown semantic IRI" in exc_info.value.errors[0]


@pytest.mark.anyio
async def test_enrich_fails_with_structured_shacl_findings_after_repair_budget():
    config = load_enrichment_config(FIXTURE_CONFIG)
    bad_timeseries = EnrichmentProposal(
        thing_types=["http://wotbot.test/dev/TemperatureSensor"],
        affordances=[
            {
                "section": "properties",
                "name": "temperature",
                "types": ["http://wotbot.test/ts/TimeSeriesMeasurement"],
            }
        ],
    )

    with pytest.raises(EnrichmentError) as exc_info:
        await enrich_thing_document(
            sample_thing_without_unit(),
            config=config,
            llm=FakeLlm([bad_timeseries]),
            max_repair_attempts=0,
        )

    assert "SHACL" in exc_info.value.errors[0]
    assert "affordance=temperature" in exc_info.value.errors[0]
    assert exc_info.value.shacl_findings
    assert exc_info.value.shacl_findings[0].focus_label == "temperature"
    assert exc_info.value.shacl_findings[0].blocks_enrichment
