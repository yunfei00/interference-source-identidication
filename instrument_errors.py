from __future__ import annotations


class InstrumentCommunicationError(IOError):
    """A transport/session failure attributed to one instrument."""

    instrument = "INSTRUMENT"

    def __init__(self, operation: str, cause: BaseException | str):
        self.operation = operation
        self.cause = cause
        super().__init__(f"{self.instrument} communication failure during {operation}: {cause}")


class N9020ACommunicationError(InstrumentCommunicationError):
    instrument = "N9020A"


class ScopeCommunicationError(InstrumentCommunicationError):
    instrument = "SDS3104XHD"


def is_communication_exception(exc: BaseException) -> bool:
    """Return whether *exc* represents transport I/O rather than bad business data."""
    if isinstance(exc, InstrumentCommunicationError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        from pyvisa.errors import VisaIOError
    except ImportError:
        VisaIOError = ()  # type: ignore[assignment,misc]
    return isinstance(exc, VisaIOError)
