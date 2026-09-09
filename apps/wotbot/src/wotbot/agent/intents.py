from dataclasses import dataclass
from enum import StrEnum

OUTPUT_MODES = [
    "text/plain",
    "application/json",
    "image/png",
    "image/jpeg",
    "application/vnd.plotly.v1+json",
    "application/octet-stream",
]
_SKILLS = [
    (
        "chat",
        "Conversation",
        "Answer questions about Things and smart living.",
        "What can you help me with?",
    ),
    (
        "control",
        "Device control",
        "Read and operate registered Things and create control panels.",
        "Turn off the living room lights.",
    ),
    (
        "analysis",
        "Analysis",
        "Analyze Thing data and generate charts, files and dashboards.",
        "Analyze yesterday's energy use and create a dashboard.",
    ),
    (
        "jobs",
        "Automation jobs",
        "Create and manage scheduled or event-driven automations.",
        "Schedule the lights to turn off at 11 pm.",
    ),
    (
        "virtual_things",
        "Virtual Things",
        "Create and manage virtual Things backed by existing capabilities.",
        "Create a virtual Thing for total home power.",
    ),
    (
        "discovery",
        "Discovery",
        "Find Things and external sources and inspect their capabilities.",
        "Find temperature sensors and show their properties.",
    ),
]


@dataclass(frozen=True)
class Intent:
    id: str
    name: str
    description: str
    example: str
    entry: str
    outputs: tuple[str, ...]


INTENTS = {
    id: Intent(
        id,
        name,
        description,
        example,
        "respond" if id == "chat" else id + "_llm",
        tuple(OUTPUT_MODES if id == "analysis" else ["text/plain", "application/json"]),
    )
    for id, name, description, example in _SKILLS
}
IntentId = StrEnum("IntentId", {name: name for name in INTENTS})
SKILLS = _SKILLS
