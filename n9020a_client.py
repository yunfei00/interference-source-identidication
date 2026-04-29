from __future__ import annotations

from dataclasses import dataclass


@dataclass
class N9020AConfig:
    resource: str
    timeout_ms: int = 5000
    remote_csv_path: str = r"D:\\data.csv"


class N9020AClient:
    """A small wrapper around PyVISA for N9020A communication."""

    def __init__(self, config: N9020AConfig):
        self.config = config
        self._rm = None
        self._inst = None

    def connect(self) -> None:
        import pyvisa

        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.config.resource)
        self._inst.timeout = self.config.timeout_ms
        # Optional sanity check
        _ = self.query("*IDN?")

    def disconnect(self) -> None:
        if self._inst is not None:
            self._inst.close()
            self._inst = None
        if self._rm is not None:
            self._rm.close()
            self._rm = None

    def write(self, cmd: str) -> None:
        if self._inst is None:
            raise RuntimeError("Instrument not connected")
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        if self._inst is None:
            raise RuntimeError("Instrument not connected")
        return str(self._inst.query(cmd)).strip()

    def fetch_csv_text(self) -> str:
        """Fetch measurement as CSV text from instrument memory.

        SCPI sequence:
        1) Disable continuous scan.
        2) Trigger one immediate scan.
        3) Store trace data to instrument file.
        4) Read file back via MMEM:DATA?.
        """
        if self._inst is None:
            raise RuntimeError("Instrument not connected")

        remote_path = self.config.remote_csv_path
        self.write(":INIT:CONT OFF")
        self.write(":INIT:IMM")
        self.write(f'MMEM:STOR:TRAC:DATA TRAC1, "{remote_path}"')
        raw = self.query(f':MMEM:DATA? "{remote_path}"')
        if not raw:
            raise RuntimeError("Empty data returned from instrument")

        # The returned payload may already be CSV text; normalize line endings.
        csv_text = raw.replace("\r\n", "\n").strip()
        return csv_text
