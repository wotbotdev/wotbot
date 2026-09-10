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
        "Explain WoTBot capabilities and answer general questions about connected devices, APIs and datasets.",
        "What can you help me with?",
    ),
    (
        "control",
        "Thing control",
        "Read properties, write values and invoke actions on registered Things, including devices and APIs. Create interactive control panels when requested.",
        "Turn off the living room lights.",
    ),
    (
        "analysis",
        "Analysis",
        "Retrieve and analyze data from registered Things and onboarded datasets. Run Python calculations and produce charts, downloadable files or interactive dashboards.",
        "Analyze yesterday's energy use and create a dashboard.",
    ),
    (
        "jobs",
        "Automation jobs",
        "Create, list, run or delete automation jobs using natural-language instructions or Python. Support one-time, recurring and event-triggered runs, including structured record collection.",
        "Schedule the lights to turn off at 11 pm.",
    ),
    (
        "virtual_things",
        "Virtual Things",
        "Create and manage virtual Things with computed properties, Python actions and emitted events. Combine existing Thing capabilities and shared state into a reusable interface.",
        "Create a virtual Thing that sums the power readings from three meters.",
    ),
    (
        "discovery",
        "Discovery",
        "Find registered Things and inspect their properties, actions and events. Search registered external catalogs for APIs or datasets, onboard selected results as Things, and request registration of new sources.",
        "Find a traffic dataset in a registered external catalog and add it as a Thing.",
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
