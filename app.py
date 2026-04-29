from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from n9020a_client import N9020AClient, N9020AConfig

STATE_FILE = Path("collector_state.json")


@dataclass
class CollectorState:
    address: str = ""
    folder: str = ""
    interval_sec: int = 5
    total_count: int = 100
    current_index: int = 1


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("N9020A 定时 CSV 采集")

        self.client: N9020AClient | None = None
        self.connected = False
        self.running = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._collect_once)

        self.state = self._load_state()
        self._build_ui()
        self._load_state_to_ui()
        self._refresh_progress()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        form = QFormLayout()
        self.address_edit = QLineEdit()
        form.addRow("仪表地址", self.address_edit)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_btn = QPushButton("选择...")
        self.folder_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(self.folder_btn)
        form.addRow("存储文件夹", folder_row)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3600)
        form.addRow("采集间隔(秒)", self.interval_spin)

        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 1_000_000)
        form.addRow("采集总数", self.total_spin)

        btn_row = QHBoxLayout()
        self.connect_btn = QPushButton("连接仪表")
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.start_btn = QPushButton("开始采集")
        self.start_btn.clicked.connect(self._start_collect)
        self.stop_btn = QPushButton("中断采集")
        self.stop_btn.clicked.connect(self._stop_collect)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)

        self.status_label = QLabel("状态：未连接")
        self.progress_label = QLabel("进度：0 / 0")
        self.next_file_label = QLabel("下一个文件：000001.csv")
        self.status_label.setAlignment(Qt.AlignLeft)

        layout = QVBoxLayout(central)
        layout.addLayout(form)
        layout.addLayout(btn_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.next_file_label)

    def _load_state(self) -> CollectorState:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return CollectorState(**data)
            except Exception:
                pass
        return CollectorState()

    def _save_state(self) -> None:
        self.state.address = self.address_edit.text().strip()
        self.state.folder = self.folder_edit.text().strip()
        self.state.interval_sec = self.interval_spin.value()
        self.state.total_count = self.total_spin.value()
        STATE_FILE.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_state_to_ui(self) -> None:
        self.address_edit.setText(self.state.address)
        self.folder_edit.setText(self.state.folder)
        self.interval_spin.setValue(self.state.interval_sec)
        self.total_spin.setValue(self.state.total_count)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if folder:
            self.folder_edit.setText(folder)
            self._save_state()
            self._sync_index_with_folder()
            self._refresh_progress()

    def _toggle_connect(self) -> None:
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        addr = self.address_edit.text().strip()
        if not addr:
            QMessageBox.warning(self, "提示", "请先填写仪表地址")
            return

        try:
            self.client = N9020AClient(N9020AConfig(resource=addr))
            self.client.connect()
            self.connected = True
            self.connect_btn.setText("断开仪表")
            self.status_label.setText("状态：已连接")
            self._save_state()
        except Exception as exc:
            self.client = None
            QMessageBox.critical(self, "连接失败", str(exc))

    def _disconnect(self) -> None:
        self._stop_collect()
        if self.client:
            self.client.disconnect()
        self.client = None
        self.connected = False
        self.connect_btn.setText("连接仪表")
        self.status_label.setText("状态：未连接")

    def _sync_index_with_folder(self) -> None:
        folder = Path(self.folder_edit.text().strip())
        if not folder.exists():
            return
        max_idx = 0
        for f in folder.glob("*.csv"):
            try:
                idx = int(f.stem)
                max_idx = max(max_idx, idx)
            except ValueError:
                continue
        self.state.current_index = max_idx + 1 if max_idx > 0 else 1
        self._save_state()

    def _refresh_progress(self) -> None:
        current = max(self.state.current_index - 1, 0)
        total = self.total_spin.value()
        self.progress_label.setText(f"进度：{current} / {total}")
        self.next_file_label.setText(f"下一个文件：{self.state.current_index:06d}.csv")

    def _start_collect(self) -> None:
        if not self.connected or self.client is None:
            QMessageBox.warning(self, "提示", "请先连接仪表")
            return

        folder = Path(self.folder_edit.text().strip())
        if not str(folder):
            QMessageBox.warning(self, "提示", "请先选择存储文件夹")
            return
        folder.mkdir(parents=True, exist_ok=True)

        self._sync_index_with_folder()
        if self.state.current_index > self.total_spin.value():
            QMessageBox.information(self, "完成", "已达到采集总数，无需继续")
            self._refresh_progress()
            return

        self.running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("状态：采集中")
        self._save_state()
        self.timer.start(self.interval_spin.value() * 1000)
        self._collect_once()

    def _stop_collect(self) -> None:
        self.running = False
        self.timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("状态：已停止")
        self._save_state()

    def _collect_once(self) -> None:
        if not self.running or self.client is None:
            return

        if self.state.current_index > self.total_spin.value():
            self._stop_collect()
            QMessageBox.information(self, "完成", "采集任务完成")
            return

        try:
            csv_text = self.client.fetch_csv_text()
            folder = Path(self.folder_edit.text().strip())
            filename = folder / f"{self.state.current_index:06d}.csv"
            header = f"timestamp,{datetime.now().isoformat()}\n"
            filename.write_text(header + csv_text + "\n", encoding="utf-8")
            self.state.current_index += 1
            self._save_state()
            self._refresh_progress()
        except Exception as exc:
            self._stop_collect()
            QMessageBox.critical(self, "采集失败", str(exc))

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_state()
        self._disconnect()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(640, 260)
    w.show()
    sys.exit(app.exec())
