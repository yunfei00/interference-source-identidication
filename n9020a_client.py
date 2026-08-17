from __future__ import annotations

from dataclasses import dataclass

from instrument_errors import N9020ACommunicationError, is_communication_exception


@dataclass
class N9020AConfig:
    resource: str
    timeout_ms: int
    remote_csv_path: str = r"D:\\data.csv"
    reconnect_enabled: bool = True
    reconnect_delay_sec: float = 15.0
    reconnect_max_attempts: int = 5


class N9020AClient:
    """A small wrapper around PyVISA for N9020A communication."""

    def __init__(self, config: N9020AConfig):
        self.config = config
        self._rm = None
        self._inst = None
        self._idn = ""

    def connect(self) -> None:
        import pyvisa

        try:
            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(self.config.resource)
            self._inst.timeout = self.config.timeout_ms
            self._idn = self.query("*IDN?")
        except Exception as exc:
            self.disconnect()
            if is_communication_exception(exc):
                raise N9020ACommunicationError("connect/*IDN?", exc) from exc
            raise

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            finally:
                self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            finally:
                self._rm = None
        self._idn = ""

    def is_connected(self) -> bool:
        return self._inst is not None

    def reconnect(self) -> str:
        """Create a fresh VISA session and return the verified identity string."""
        self.disconnect()
        self.connect()
        return self._idn

    def write(self, cmd: str) -> None:
        if self._inst is None:
            raise N9020ACommunicationError(cmd, "Instrument not connected")
        try:
            self._inst.write(cmd)
        except Exception as exc:
            if is_communication_exception(exc):
                raise N9020ACommunicationError(cmd, exc) from exc
            raise

    def query(self, cmd: str) -> str:
        if self._inst is None:
            raise N9020ACommunicationError(cmd, "Instrument not connected")
        try:
            return str(self._inst.query(cmd)).strip()
        except Exception as exc:
            if is_communication_exception(exc):
                raise N9020ACommunicationError(cmd, exc) from exc
            raise

    def fetch_csv_text(self) -> str:
        """Fetch measurement as CSV text from instrument memory.

        SCPI sequence:
        1) Disable continuous scan.
        2) Trigger one immediate scan.
        3) Wait for the scan to complete.
        4) Store trace data to instrument file.
        5) Read file back via MMEM:DATA?.
        """
        if self._inst is None:
            raise N9020ACommunicationError("fetch_csv_text", "Instrument not connected")

        remote_path = self.config.remote_csv_path
        self.write(":INIT:CONT OFF")
        self.write(":INIT:IMM")
        # Kept as one isolated compatibility change: remove this query if a
        # particular firmware does not support operation-complete polling.
        self.query("*OPC?")
        self.write(f'MMEM:STOR:TRAC:DATA TRACE1, "{remote_path}"')
        raw = self.query(f':MMEM:DATA? "{remote_path}"')
        if not raw:
            raise N9020ACommunicationError("MMEM:DATA?", "empty data returned")

        # Readback succeeded; delete temporary file on the instrument.
        self.write(f'MMEM:DEL "{remote_path}"')

        # The returned payload may already be CSV text; normalize line endings.
        csv_text = raw.replace("\r\n", "\n").strip()
        return csv_text

    def set_center_and_span_mhz(self, center_mhz: float, span_mhz: float) -> None:
        if self._inst is None:
            raise N9020ACommunicationError(
                "set_center_and_span_mhz", "Instrument not connected"
            )
        self.write(f":FREQ:CENT {center_mhz} MHz")
        self.write(f":FREQ:SPAN {span_mhz} MHz")
