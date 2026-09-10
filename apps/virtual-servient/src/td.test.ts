import assert from "node:assert/strict";
import test from "node:test";

import {
  catalogTdWithMetadata,
  concreteCatalogTd,
  tdForProduce,
} from "./td.js";

test("tdForProduce makes legacy virtual properties read-only and preserves schemas", () => {
  for (const flags of [{}, { readOnly: false }, { writeOnly: true }]) {
    const property = {
      type: "number",
      ...flags,
      forms: [{ href: "urn:abstract", op: ["readproperty"] }],
    };
    const original = structuredClone(property);
    const action = { input: { type: "number" }, output: { type: "number" } };
    const event = { data: { type: "number" } };
    const td = tdForProduce({
      id: "virtual:things:measurement",
      title: "Measurement",
      properties: { metres: property },
      actions: { add: action },
      events: { crossed: event },
    });

    assert.deepEqual(td.properties, {
      metres: { type: "number", readOnly: true, writeOnly: false },
    });
    assert.deepEqual(td.actions, { add: action });
    assert.deepEqual(td.events, { crossed: event });
    assert.deepEqual(property, original);
  }
});

test("concreteCatalogTd keeps fallback affordances missing from exposed TD", async () => {
  const td = await concreteCatalogTd(
    {
      getThingDescription: () => ({
        id: "virtual:things:multi",
        title: "Multi",
        properties: {
          temperature: { type: "number" },
        },
      }),
    },
    {
      id: "virtual:things:multi",
      title: "Multi",
      properties: {
        temperature: { type: "number" },
        humidity: { type: "number" },
      },
      actions: {
        refresh: { output: { type: "object" } },
        reset: { input: { type: "object" }, output: { type: "object" } },
      },
      events: {
        tick: { data: { type: "object" } },
        alarm: { data: { type: "object" } },
      },
    },
    "virtual:things:multi",
  );

  assert.deepEqual(Object.keys(td.properties as object).sort(), [
    "humidity",
    "temperature",
  ]);
  assert.deepEqual(Object.keys(td.actions as object).sort(), [
    "refresh",
    "reset",
  ]);
  assert.deepEqual(Object.keys(td.events as object).sort(), ["alarm", "tick"]);
});

test("catalogTdWithMetadata applies new metadata but preserves concrete forms", () => {
  const current = {
    id: "virtual:things:sensor",
    title: "Sensor",
    properties: {
      temperature: {
        type: "number",
        forms: [
          {
            href: "http://host:3014/virtual:things:sensor/properties/temperature",
            op: ["readproperty"],
          },
        ],
      },
    },
    security: ["nosec_sc"],
    securityDefinitions: { nosec_sc: { scheme: "nosec" } },
  };
  const desired = {
    id: "virtual:things:sensor",
    title: "Sensor",
    description: "Enriched description",
    "@type": "saref:TemperatureSensor",
    properties: {
      temperature: {
        type: "number",
        title: "Ambient temperature",
        // Abstract form as stored in the definition; must be replaced.
        forms: [{ href: "urn:abstract", op: ["readproperty"] }],
      },
    },
  };

  const result = catalogTdWithMetadata(
    current,
    desired,
    "virtual:things:sensor",
  );

  assert.equal(result.description, "Enriched description");
  assert.equal(result["@type"], "saref:TemperatureSensor");
  const property = (result.properties as Record<string, any>).temperature;
  assert.equal(property.title, "Ambient temperature");
  // The concrete (http) form is kept; the abstract urn: form is dropped.
  assert.equal(property.forms.length, 1);
  assert.equal(
    property.forms[0].href,
    "http://host:3014/virtual:things:sensor/properties/temperature",
  );
});
