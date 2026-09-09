"""A2A compatibility presentation over shared output processing."""

from wotbot.a2a.adapter import to_wire
from wotbot.agent_api.outputs import ArtifactCollector as NeutralCollector
from wotbot.agent_api.outputs import executor_headers as executor_headers
from wotbot.agent_api.outputs import fetch_artifact_metadata as fetch_artifact_metadata
from wotbot.agent_api.outputs import fetch_plotly_figure as fetch_plotly_figure


class ArtifactCollector:
    def __init__(self, **kwargs):
        self.neutral = NeutralCollector(**kwargs)

    def __getattr__(self, name):
        return getattr(self.neutral, name)

    async def consume(self, event, *, seen):
        return [to_wire(a) for a in await self.neutral.consume(event, seen=seen)]
