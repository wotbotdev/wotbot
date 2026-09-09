"""Compatibility adapter for A2A callers of the shared task store."""

from dataclasses import replace

from google.protobuf.json_format import MessageToDict

from wotbot.a2a.adapter import query, to_wire, translate
from wotbot.agent_api.store import TaskStore as AgentTaskStore
from wotbot.agent_api.types import Task as AgentTask


class TaskStore:
    """Wire-format view of the neutral store.

    Only the four methods that cross the A2A boundary convert; the rest of the
    neutral store's surface is protocol-independent and forwarded verbatim.
    """

    def __init__(self, *, neutral=None, **kwargs):
        self.neutral = neutral or AgentTaskStore(**kwargs)

    def thread_id(self, owner, task_id):
        return self.neutral.thread_id(owner, task_id)

    def prune(self):
        return self.neutral.prune()

    def delete_contexts(self, thread_ids):
        return self.neutral.delete_contexts(thread_ids)

    @translate
    def admit(self, owner, request):
        admission = self.neutral.admit(owner, request)
        return replace(admission, task=to_wire(admission.task))

    @translate
    def get(self, owner, task_id):
        return to_wire(self.neutral.get(owner, task_id, "assistant"))

    @translate
    def save(self, owner, task, pending=None):
        self.neutral.save(owner, AgentTask.model_validate(MessageToDict(task)), pending)

    @translate
    def list(self, owner, params):
        tasks, total, cursor = self.neutral.list(owner, query(params))
        return [to_wire(task) for task in tasks], total, cursor
