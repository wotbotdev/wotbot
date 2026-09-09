"""A2A error translation for the shared interruption contract."""

from wotbot.a2a.adapter import translate
from wotbot.agent_api.interrupts import describe_interrupt as describe_interrupt
from wotbot.agent_api.interrupts import validate_reply as shared_validate_reply


@translate
def validate_reply(pending, parts):
    return shared_validate_reply(pending, parts)
