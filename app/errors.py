class TeamError(Exception):
    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


class NotFound(TeamError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "not_found")


class ApprovalRequired(TeamError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "approval_required")


class SelfWakeRejected(TeamError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "self_wake_rejected")


class BudgetExceeded(TeamError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "budget")
