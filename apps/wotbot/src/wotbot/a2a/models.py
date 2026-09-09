"""Compatibility names for application-owned records in the existing tables."""

from wotbot.agent_api.models import AgentTaskRecord as A2ATaskRecord
from wotbot.agent_api.models import AgentMessageRecord as A2AMessageRecord
from wotbot.agent_api.models import AgentArtifactRecord as A2AArtifactRecord
from wotbot.agent_api.models import AgentSubscriptionRecord
from wotbot.agent_api.models import RESERVED_STATES_SQL

__all__ = [
    "A2ATaskRecord",
    "A2AMessageRecord",
    "A2AArtifactRecord",
    "AgentSubscriptionRecord",
    "RESERVED_STATES_SQL",
]
