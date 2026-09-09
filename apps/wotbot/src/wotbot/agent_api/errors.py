"""Application errors mapped to each transport at its boundary."""


class AgentError(ValueError):
    pass


class InvalidParamsError(AgentError):
    pass


class TaskNotFoundError(AgentError):
    def __init__(self, message="Task not found"):
        super().__init__(message)


class TaskNotCancelableError(AgentError):
    def __init__(self, message="Task is not cancellable"):
        super().__init__(message)


class UnsupportedOperationError(AgentError):
    pass
