from __future__ import annotations

import errno
from enum import Enum


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


class CommunicationFailureKind(str, Enum):
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


_NETWORK_ERRNOS = {
    errno.EPIPE,
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ENETRESET,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
    errno.ENOTCONN,
    errno.ETIMEDOUT,
}


def classify_communication_failure(exc: BaseException | str) -> CommunicationFailureKind:
    """Classify recovery timing without labelling every I/O failure as calibration."""
    if isinstance(exc, InstrumentCommunicationError):
        return classify_communication_failure(exc.cause)
    if isinstance(exc, TimeoutError):
        return CommunicationFailureKind.TIMEOUT
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return CommunicationFailureKind.DISCONNECTED
    if isinstance(exc, OSError):
        if exc.errno in _NETWORK_ERRNOS:
            # A socket ETIMEDOUT is a connectivity failure; VISA's explicit timeout
            # code below is the temporary-busy/calibration case.
            return CommunicationFailureKind.DISCONNECTED

    try:
        from pyvisa.constants import StatusCode
        from pyvisa.errors import VisaIOError
    except ImportError:
        VisaIOError = ()  # type: ignore[assignment,misc]
        StatusCode = None  # type: ignore[assignment]
    if VisaIOError and isinstance(exc, VisaIOError):
        error_code = getattr(exc, "error_code", None)
        if StatusCode is not None and error_code == StatusCode.error_timeout:
            return CommunicationFailureKind.TIMEOUT
        connection_codes = {
            getattr(StatusCode, name, None)
            for name in (
                "error_connection_lost",
                "error_resource_not_found",
                "error_invalid_resource_name",
            )
        }
        if error_code in connection_codes:
            return CommunicationFailureKind.DISCONNECTED

    # Some backends expose only a textual cause. Keep this as a conservative
    # fallback after structured exception and error-code checks.
    text = str(exc).casefold()
    if any(
        marker in text
        for marker in (
            "connection reset",
            "connection lost",
            "network unreachable",
            "no route",
            "broken pipe",
            "not connected",
            "host unreachable",
            "socket disconnect",
        )
    ):
        return CommunicationFailureKind.DISCONNECTED
    if "timeout" in text or "timed out" in text:
        return CommunicationFailureKind.TIMEOUT
    return CommunicationFailureKind.UNKNOWN


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
