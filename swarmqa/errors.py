"""Stable error types shared by every chunk."""


class AQAError(Exception):
    """Base error for Autonomous QA."""


class ConfigError(AQAError):
    """Invalid campaign configuration. `errors` holds field-level messages."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors) if self.errors else "invalid config")


class IntentError(AQAError):
    """Intent set could not be ingested."""


class AppMissingError(AQAError):
    """App path is missing, unsigned, or failed to launch."""


class AppCrashedError(AQAError):
    """App exited or crashed during the session."""


class ElementNotFoundError(AQAError):
    """No accessibility element matched the query."""


class UITimeoutError(AQAError):
    """Timed out waiting for UI."""


class BackendUnavailable(AQAError):
    """Selected runner backend cannot run on this host."""


class ChunkNotReady(AQAError):
    """A feature module is still a contract stub."""

    def __init__(self, chunk: str, detail: str = ""):
        self.chunk = chunk
        message = f"{chunk} is not implemented"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
