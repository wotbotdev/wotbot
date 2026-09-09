"""Compatibility adapter for A2A callers of the shared task store."""

from dataclasses import replace

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict

from wotbot.a2a.adapter import query, to_wire, translate
from wotbot.agent_api.store import ACTIVE as ACTIVE
from wotbot.agent_api.store import PAUSED as PAUSED
from wotbot.agent_api.store import RESERVED as RESERVED
from wotbot.agent_api.store import TaskStore as AgentTaskStore
from wotbot.agent_api.store import request_fingerprint as request_fingerprint
from wotbot.agent_api.store import task_identity as task_identity
from wotbot.agent_api.types import Task as AgentTask


def task_json(task):
    return MessageToDict(task)


def task_from_json(payload):
    return ParseDict({k: v for k, v in payload.items() if k != "payloadVersion"}, Task())


class TaskStore:
    def __init__(self, *, neutral=None, **kwargs):
        self.neutral = neutral or AgentTaskStore(**kwargs)

    def __getattr__(self, name):
        return getattr(self.neutral, name)

    @translate
    def admit(self, owner, request):
        admission = self.neutral.admit(owner, request)
        return replace(admission, task=to_wire(admission.task))

    @translate
    def get(self, owner, task_id):
        self.neutral.require_family(owner, task_id, "assistant")
        return to_wire(self.neutral.get(owner, task_id))

    @translate
    def save(self, owner, task, pending=None):
        self.neutral.save(owner, AgentTask.model_validate(MessageToDict(task)), pending)

    @translate
    def list(self, owner, params):
        tasks, total, cursor = self.neutral.list(owner, query(params))
        return [to_wire(task) for task in tasks], total, cursor
