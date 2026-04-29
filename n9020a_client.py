from __future__ import annotations

from dataclasses import dataclass


@dataclass
class N9020AConfig:
    resource: str
    timeout_ms: int = 5000


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
        """Fetch measurement as CSV text.

        NOTE: Different firmware/modes may require different SCPI sequences.
        Adjust this function according to your instrument setup.
        """
        if self._inst is None:
            raise RuntimeError("Instrument not connected")

        # Common approach: ask trace data and convert to CSV lines.
        # For N9020A, TRACE:DATA? usually returns comma-separated values.
        raw = self.query("TRACE:DATA?")
        if not raw:
            raise RuntimeError("Empty data returned from instrument")

        return "value\n" + "\n".join(raw.split(","))
