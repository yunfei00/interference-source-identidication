from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
import shutil

from PySide6.QtCore import QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QDoubleSpinBox,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from acquisition_worker import AcquisitionWorker, CaptureRequest
from file_pairing import is_capture_complete, next_capture_index
from n9020a_client import N9020AClient, N9020AConfig
from sds3104xhd_client import SDS3104XHDClient, SDS3104XHDConfig

STATE_FILE = Path("collector_state.json")


@dataclass
class CollectorState:
    address: str = ""
    folder: str = ""
    interval_sec: int = 5
    total_count: int = 100
    current_index: int = 1
    remote_csv_path: str = r"D:\\data.csv"
    time_domain_enabled: bool = False
    td_start_mhz: float = 600.0
    td_step_mhz: float = 5.0
    td_stop_mhz: float = 700.0
    td_current_freq_mhz: float = 600.0
    scope_enabled: bool = True
    scope_ip: str = "192.168.1.50"
    scope_channel: str = "C1"
    scope_single_timeout_sec: int = 30


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("N9020A 定时 CSV 采集")

        self.client: N9020AClient | None = None
        self.connected = False
        self.scope_client: SDS3104XHDClient | None = None
        self.scope_connected = False
        self.scope_idn = ""
        self.running = False
        self._closing = False
        self._loading_ui = False
        self._worker_thread: QThread | None = None
        self._worker: AcquisitionWorker | None = None
        self._worker_had_error = False
        self._capture_completed_task = False

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._collect_once)

        self.state = self._load_state()
        self._build_ui()
        self._load_state_to_ui()
        self._refresh_progress()
        self._update_controls()

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
        self.open_folder_btn = QPushButton("打开文件夹")
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.clear_folder_btn = QPushButton("清空文件夹")
        self.clear_folder_btn.clicked.connect(self._clear_folder)
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(self.folder_btn)
        folder_row.addWidget(self.open_folder_btn)
        folder_row.addWidget(self.clear_folder_btn)
        form.addRow("存储文件夹", folder_row)

        self.remote_csv_edit = QLineEdit()
        form.addRow("仪表CSV路径", self.remote_csv_edit)

        scope_title = QLabel("<b>SDS3104X HD</b>")
        form.addRow(scope_title)

        self.scope_enabled_check = QCheckBox("启用示波器采集")
        self.scope_enabled_check.toggled.connect(self._on_scope_enabled_toggled)
        form.addRow("联合采集", self.scope_enabled_check)

        self.scope_ip_edit = QLineEdit()
        form.addRow("Scope IP", self.scope_ip_edit)

        self.scope_channel_combo = QComboBox()
        self.scope_channel_combo.addItems(["C1", "C2", "C3", "C4"])
        form.addRow("Scope Channel", self.scope_channel_combo)

        self.scope_timeout_spin = QSpinBox()
        self.scope_timeout_spin.setRange(1, 3600)
        self.scope_timeout_spin.setSuffix(" 秒")
        form.addRow("Single 超时", self.scope_timeout_spin)

        scope_btn_row = QHBoxLayout()
        self.scope_connect_btn = QPushButton("连接示波器")
        self.scope_connect_btn.clicked.connect(self._connect_scope)
        self.scope_disconnect_btn = QPushButton("断开示波器")
        self.scope_disconnect_btn.clicked.connect(self._disconnect_scope)
        self.scope_disconnect_btn.setEnabled(False)
        scope_btn_row.addWidget(self.scope_connect_btn)
        scope_btn_row.addWidget(self.scope_disconnect_btn)
        form.addRow("Scope 连接", scope_btn_row)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 3600)
        form.addRow("采集间隔(秒)", self.interval_spin)

        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 1_000_000)
        form.addRow("采集总数", self.total_spin)

        self.time_domain_check = QCheckBox("启用 N9020A Zero Span 扫频采集")
        self.time_domain_check.toggled.connect(self._on_time_domain_toggled)
        form.addRow("采集模式", self.time_domain_check)

        self.td_start_spin = QDoubleSpinBox()
        self.td_start_spin.setRange(0.0, 50000.0)
        self.td_start_spin.setDecimals(3)
        self.td_start_spin.setSuffix(" MHz")
        form.addRow("时域起始频率", self.td_start_spin)

        self.td_step_spin = QDoubleSpinBox()
        self.td_step_spin.setRange(0.001, 10000.0)
        self.td_step_spin.setDecimals(3)
        self.td_step_spin.setSuffix(" MHz")
        form.addRow("时域步进步长", self.td_step_spin)

        self.td_stop_spin = QDoubleSpinBox()
        self.td_stop_spin.setRange(0.0, 50000.0)
        self.td_stop_spin.setDecimals(3)
        self.td_stop_spin.setSuffix(" MHz")
        form.addRow("时域终止频率", self.td_stop_spin)

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
        self.current_freq_label = QLabel("当前频率：--")
        self.scope_status_label = QLabel("Scope：未连接")
        self.status_label.setAlignment(Qt.AlignLeft)

        layout = QVBoxLayout(central)
        layout.addLayout(form)
        layout.addLayout(btn_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.next_file_label)
        layout.addWidget(self.current_freq_label)
        layout.addWidget(self.scope_status_label)

    def _load_state(self) -> CollectorState:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                known_fields = {field.name for field in fields(CollectorState)}
                return CollectorState(**{key: value for key, value in data.items() if key in known_fields})
            except Exception:
                pass
        return CollectorState()

    def _save_state(self) -> None:
        self.state.address = self.address_edit.text().strip()
        self.state.folder = self.folder_edit.text().strip()
        self.state.interval_sec = self.interval_spin.value()
        self.state.total_count = self.total_spin.value()
        self.state.remote_csv_path = self.remote_csv_edit.text().strip() or r"D:\\data.csv"
        self.state.time_domain_enabled = self.time_domain_check.isChecked()
        self.state.td_start_mhz = self.td_start_spin.value()
        self.state.td_step_mhz = self.td_step_spin.value()
        self.state.td_stop_mhz = self.td_stop_spin.value()
        self.state.scope_enabled = self.scope_enabled_check.isChecked()
        self.state.scope_ip = self.scope_ip_edit.text().strip() or "192.168.1.50"
        self.state.scope_channel = self.scope_channel_combo.currentText()
        self.state.scope_single_timeout_sec = self.scope_timeout_spin.value()
        STATE_FILE.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_state_to_ui(self) -> None:
        self._loading_ui = True
        try:
            self.address_edit.setText(self.state.address)
            self.folder_edit.setText(self.state.folder)
            self.interval_spin.setValue(self.state.interval_sec)
            self.total_spin.setValue(self.state.total_count)
            self.remote_csv_edit.setText(self.state.remote_csv_path or r"D:\\data.csv")
            self.time_domain_check.setChecked(self.state.time_domain_enabled)
            self.td_start_spin.setValue(self.state.td_start_mhz)
            self.td_step_spin.setValue(self.state.td_step_mhz)
            self.td_stop_spin.setValue(self.state.td_stop_mhz)
            self.scope_enabled_check.setChecked(self.state.scope_enabled)
            self.scope_ip_edit.setText(self.state.scope_ip)
            channel_index = self.scope_channel_combo.findText(self.state.scope_channel)
            self.scope_channel_combo.setCurrentIndex(max(channel_index, 0))
            self.scope_timeout_spin.setValue(self.state.scope_single_timeout_sec)
            self._on_time_domain_toggled(self.state.time_domain_enabled)
            self._on_scope_enabled_toggled(self.state.scope_enabled)
        finally:
            self._loading_ui = False

    def _on_time_domain_toggled(self, checked: bool) -> None:
        self.td_start_spin.setEnabled(checked)
        self.td_step_spin.setEnabled(checked)
        self.td_stop_spin.setEnabled(checked)
        if not self._loading_ui:
            self._save_state()
            self._sync_index_with_folder()
            self._refresh_progress()

    def _on_scope_enabled_toggled(self, _checked: bool) -> None:
        if not self._loading_ui:
            self._save_state()
            self._sync_index_with_folder()
            self._refresh_progress()

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
            remote_csv_path = self.remote_csv_edit.text().strip() or r"D:\\data.csv"
            self.client = N9020AClient(N9020AConfig(resource=addr, remote_csv_path=remote_csv_path))
            self.client.connect()
            self.connected = True
            self.connect_btn.setText("断开仪表")
            self.status_label.setText("状态：已连接")
            self._save_state()
            self._update_controls()
        except Exception as exc:
            if self.client is not None:
                self.client.disconnect()
            self.client = None
            QMessageBox.critical(self, "连接失败", str(exc))

    def _connect_scope(self) -> None:
        ip = self.scope_ip_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "提示", "请先填写示波器 IP")
            return

        config = SDS3104XHDConfig(
            ip=ip,
            channel=self.scope_channel_combo.currentText(),
            single_timeout_sec=self.scope_timeout_spin.value(),
        )
        candidate = SDS3104XHDClient(config)
        try:
            candidate.connect()
            self.scope_idn = candidate.identify()
            self.scope_client = candidate
            self.scope_connected = True
            self.scope_status_label.setText(f"Scope：{self.scope_idn}")
            self._save_state()
            self._update_controls()
        except Exception as exc:
            candidate.disconnect()
            self.scope_client = None
            self.scope_connected = False
            self.scope_idn = ""
            self.scope_status_label.setText("Scope：未连接")
            QMessageBox.critical(self, "示波器连接失败", str(exc))

    def _disconnect_scope(self) -> None:
        self._stop_collect()
        if self.scope_client:
            self.scope_client.disconnect()
        self.scope_client = None
        self.scope_connected = False
        self.scope_idn = ""
        self.scope_status_label.setText("Scope：未连接")
        self._update_controls()

    def _open_folder(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            QMessageBox.warning(self, "提示", "请先选择存储文件夹")
            return
        folder = Path(folder_text)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def _clear_folder(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            QMessageBox.warning(self, "提示", "请先选择存储文件夹")
            return
        folder = Path(folder_text)
        if not folder.exists():
            QMessageBox.information(self, "提示", "文件夹不存在，无需清空")
            return
        reply = QMessageBox.question(
            self,
            "确认清空",
            f"确认删除 {folder} 下所有内容吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for item in folder.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        self.state.current_index = 1
        self.state.td_current_freq_mhz = self.td_start_spin.value()
        self._save_state()
        self._refresh_progress()

    def _disconnect(self) -> None:
        self._stop_collect()
        if self.client:
            self.client.disconnect()
        self.client = None
        self.connected = False
        self.connect_btn.setText("连接仪表")
        self.status_label.setText("状态：未连接")
        self._update_controls()

    def _sync_index_with_folder(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            return
        folder = Path(folder_text)
        scope_enabled = self.scope_enabled_check.isChecked()
        if self.time_domain_check.isChecked():
            self._set_next_zero_span_capture(folder, self.td_start_spin.value(), scope_enabled)
            self._save_state()
            return
        self.state.current_index = next_capture_index(folder, scope_enabled)
        self._save_state()

    def _set_next_zero_span_capture(
        self,
        root_folder: Path,
        start_frequency: float,
        scope_enabled: bool,
    ) -> None:
        step = self.td_step_spin.value()
        stop = self.td_stop_spin.value()
        total = self.total_spin.value()
        frequency = start_frequency
        while frequency <= stop + 1e-9:
            frequency_folder = root_folder / f"{frequency:.3f}MHz"
            index = next_capture_index(frequency_folder, scope_enabled)
            if index <= total:
                self.state.current_index = index
                self.state.td_current_freq_mhz = frequency
                return
            frequency += step
        self.state.current_index = 1
        self.state.td_current_freq_mhz = frequency

    def _refresh_progress(self) -> None:
        total = self.total_spin.value()
        if self.time_domain_check.isChecked():
            current = max(self.state.current_index - 1, 0)
            self.progress_label.setText(f"当前频点进度：{current} / {total}")
            suffix = ".csv + .npz" if self.scope_enabled_check.isChecked() else ".csv"
            self.next_file_label.setText(
                f"下一个文件：按频率目录/{self.state.current_index:06d}{suffix}"
            )
            self.current_freq_label.setText(f"当前频率：{self.state.td_current_freq_mhz:.3f} MHz")
        else:
            current = max(self.state.current_index - 1, 0)
            self.progress_label.setText(f"进度：{current} / {total}")
            suffix = ".csv + .npz" if self.scope_enabled_check.isChecked() else ".csv"
            self.next_file_label.setText(f"下一个文件：{self.state.current_index:06d}{suffix}")
            self.current_freq_label.setText("当前频率：--")

    def _start_collect(self) -> None:
        if not self.connected or self.client is None:
            QMessageBox.warning(self, "提示", "请先连接仪表")
            return

        scope_enabled = self.scope_enabled_check.isChecked()
        if scope_enabled and (not self.scope_connected or self.scope_client is None):
            QMessageBox.warning(self, "提示", "已启用示波器采集，请先连接示波器")
            return

        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            QMessageBox.warning(self, "提示", "请先选择存储文件夹")
            return
        folder = Path(folder_text)
        folder.mkdir(parents=True, exist_ok=True)

        if self.time_domain_check.isChecked():
            if self.td_start_spin.value() > self.td_stop_spin.value():
                QMessageBox.warning(self, "提示", "时域起始频率不能大于终止频率")
                return
        self._save_state()
        self._sync_index_with_folder()
        if self._is_task_complete():
            QMessageBox.information(self, "完成", "已达到采集总数，无需继续")
            self._refresh_progress()
            return

        self.running = True
        self.status_label.setText("状态：采集中")
        self._save_state()
        self._update_controls()
        self._collect_once()

    def _stop_collect(self) -> None:
        self.running = False
        self.timer.stop()
        self.status_label.setText("状态：已停止")
        self._save_state()
        self._update_controls()

    def _collect_once(self) -> None:
        if not self.running or self.client is None:
            return
        if self._worker_thread is not None:
            return
        if self._is_task_complete():
            self._finish_collection()
            return

        root_folder = Path(self.folder_edit.text().strip())
        frequency = self.state.td_current_freq_mhz if self.time_domain_check.isChecked() else None
        output_folder = root_folder / f"{frequency:.3f}MHz" if frequency is not None else root_folder
        request = CaptureRequest(
            output_folder=output_folder,
            index=self.state.current_index,
            scope_enabled=self.scope_enabled_check.isChecked(),
            frequency_mhz=frequency,
        )
        thread = QThread(self)
        worker = AcquisitionWorker(self.client, self.scope_client, request)
        worker.moveToThread(thread)
        thread.started.connect(worker.capture_one)
        worker.finished.connect(self._on_capture_finished)
        worker.error.connect(self._on_capture_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(self._on_worker_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._worker_thread = thread
        self._worker = worker
        self._worker_had_error = False
        self._capture_completed_task = False
        self._update_controls()
        thread.start()

    def _on_capture_finished(self, result: dict) -> None:
        frequency = result.get("frequency_mhz")
        captured_index = int(result["index"])
        root_folder = Path(self.folder_edit.text().strip())
        scope_enabled = self.scope_enabled_check.isChecked()
        if frequency is not None:
            current_frequency = float(frequency)
            next_index = captured_index + 1
            frequency_folder = root_folder / f"{current_frequency:.3f}MHz"
            while (
                scope_enabled
                and next_index <= self.total_spin.value()
                and is_capture_complete(frequency_folder, next_index, scope_enabled=True)
            ):
                next_index += 1
            if next_index <= self.total_spin.value():
                self.state.current_index = next_index
                self.state.td_current_freq_mhz = current_frequency
            else:
                self._set_next_zero_span_capture(
                    root_folder,
                    current_frequency + self.td_step_spin.value(),
                    scope_enabled,
                )
        else:
            next_index = captured_index + 1
            while scope_enabled and is_capture_complete(
                root_folder, next_index, scope_enabled=True
            ):
                next_index += 1
            self.state.current_index = next_index

        scope_stats = result.get("scope")
        if scope_stats:
            self.scope_status_label.setText(
                f"Scope：{self.scope_idn}\n"
                f"{scope_stats['point_count']:,} pts\n"
                f"{scope_stats['min_voltage']:.3f} ~ {scope_stats['max_voltage']:.3f} V\n"
                f"read {scope_stats['read_seconds']:.2f} s, "
                f"save {scope_stats['save_seconds']:.2f} s"
            )

        self._save_state()
        self._refresh_progress()
        self._capture_completed_task = self._is_task_complete()

    def _on_capture_error(self, message: str) -> None:
        self._worker_had_error = True
        self.running = False
        self.timer.stop()
        self.status_label.setText(f"状态：采集失败（{message}）")
        self._save_state()
        if not self._closing:
            QMessageBox.critical(self, "采集失败", message)

    def _on_worker_thread_finished(self) -> None:
        self._worker_thread = None
        self._worker = None
        self._update_controls()

        if self._closing:
            QTimer.singleShot(0, self.close)
            return
        if self._worker_had_error or not self.running:
            return
        if self._capture_completed_task:
            self._finish_collection()
            return
        self.status_label.setText(f"状态：等待 {self.interval_spin.value()} 秒后采集")
        self.timer.start(self.interval_spin.value() * 1000)

    def _is_task_complete(self) -> bool:
        if self.time_domain_check.isChecked():
            return self.state.td_current_freq_mhz > self.td_stop_spin.value() + 1e-9
        return self.state.current_index > self.total_spin.value()

    def _finish_collection(self) -> None:
        zero_span = self.time_domain_check.isChecked()
        self.running = False
        self.timer.stop()
        self.status_label.setText("状态：采集完成")
        self._save_state()
        self._update_controls()
        message = "Zero Span 采集任务完成" if zero_span else "采集任务完成"
        QMessageBox.information(self, "完成", message)

    def _update_controls(self) -> None:
        busy = self.running or self._worker_thread is not None
        self.start_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(self.running)
        self.connect_btn.setEnabled(not busy)
        self.address_edit.setEnabled(not busy and not self.connected)
        self.scope_connect_btn.setEnabled(not busy and not self.scope_connected)
        self.scope_disconnect_btn.setEnabled(not busy and self.scope_connected)
        self.scope_ip_edit.setEnabled(not busy and not self.scope_connected)
        self.scope_channel_combo.setEnabled(not busy and not self.scope_connected)
        self.scope_timeout_spin.setEnabled(not busy and not self.scope_connected)
        self.scope_enabled_check.setEnabled(not busy)
        self.folder_edit.setEnabled(not busy)
        self.folder_btn.setEnabled(not busy)
        self.clear_folder_btn.setEnabled(not busy)
        self.remote_csv_edit.setEnabled(not busy and not self.connected)
        self.interval_spin.setEnabled(not busy)
        self.total_spin.setEnabled(not busy)
        self.time_domain_check.setEnabled(not busy)
        zero_span_controls_enabled = not busy and self.time_domain_check.isChecked()
        self.td_start_spin.setEnabled(zero_span_controls_enabled)
        self.td_step_spin.setEnabled(zero_span_controls_enabled)
        self.td_stop_spin.setEnabled(zero_span_controls_enabled)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker_thread is not None:
            self._closing = True
            self._stop_collect()
            self.status_label.setText("状态：正在等待当前采集结束后退出")
            event.ignore()
            return
        self._save_state()
        if self.client:
            self.client.disconnect()
        self.client = None
        self.connected = False
        if self.scope_client:
            self.scope_client.disconnect()
        self.scope_client = None
        self.scope_connected = False
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(760, 600)
    w.show()
    sys.exit(app.exec())
