from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from data_visualizer import ConversionTask, batch_convert


class ImageConversionWorker(QObject):
    """Convert data files in a background thread, stopping between files."""

    progress = Signal(int, int)
    current_file = Signal(str)
    success_count = Signal(int)
    failure_count = Signal(int)
    skipped_count = Signal(int)
    error = Signal(str)
    log = Signal(str)
    finished = Signal(dict)

    def __init__(self, source: Path, output_directory: Path, overwrite: bool) -> None:
        super().__init__()
        self.source = source
        self.output_directory = output_directory
        self.overwrite = overwrite
        self._stop_requested = Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    @Slot()
    def run(self) -> None:
        counts = {"success": 0, "skipped": 0, "failed": 0}

        def on_start(position: int, total: int, task: ConversionTask) -> None:
            self.current_file.emit(str(task.source))
            self.log.emit(f"[{position}/{total}] converting {task.source}")

        def on_result(
            position: int,
            total: int,
            task: ConversionTask,
            status: str,
            message: str,
        ) -> None:
            counts[status] += 1
            if status == "success":
                self.log.emit(f"[{position}/{total}] {task.source.name} OK")
            elif status == "skipped":
                self.log.emit(f"[{position}/{total}] {task.source.name} SKIPPED")
            else:
                detail = f"[{position}/{total}] {task.source.name} FAILED: {message}"
                self.log.emit(detail)
                self.error.emit(detail)
            self.success_count.emit(counts["success"])
            self.skipped_count.emit(counts["skipped"])
            self.failure_count.emit(counts["failed"])
            self.progress.emit(position, total)

        try:
            summary = batch_convert(
                self.source,
                self.output_directory,
                overwrite=self.overwrite,
                should_stop=self._stop_requested.is_set,
                on_start=on_start,
                on_result=on_result,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.error.emit(message)
            summary_dict = {
                "total": 0,
                "success": 0,
                "skipped": 0,
                "failed": 1,
                "stopped": False,
                "errors": [message],
            }
        else:
            summary_dict = summary.as_dict()
        self.finished.emit(summary_dict)
