"""Domain errors raised by services and mapped to HTTP responses in app.main."""


class AppError(Exception):
    status_code = 400

    def __init__(self, detail: str, *, errors: list[dict] | None = None, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors
        self.retry_after = retry_after  # seconds, sent as Retry-After when set


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


class PermissionDeniedError(AppError):
    status_code = 403


class AuthenticationError(AppError):
    status_code = 401


class ValidationFailedError(AppError):
    status_code = 422


class PayloadTooLargeError(AppError):
    status_code = 413
