"""供应服务向 API 和 CLI 暴露的稳定错误。"""


class SupplyError(RuntimeError):
    code = "supply_error"
    status = 400


class NotFound(SupplyError):
    code = "not_found"
    status = 404


class Conflict(SupplyError):
    code = "conflict"
    status = 409


class Forbidden(SupplyError):
    code = "forbidden"
    status = 403


class InvalidState(SupplyError):
    code = "invalid_state"
    status = 409


class ValidationFailed(SupplyError):
    code = "validation_failed"
    status = 422
