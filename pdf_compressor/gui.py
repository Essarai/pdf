"""PyQt6 图形界面：递归压缩目录下所有 PDF 中的图片。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .core import OutputMode
from .worker import CompressWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF 图片压缩工具")
        self.resize(720, 560)

        self._worker: Optional[CompressWorker] = None

        self._build_ui()
        self._update_output_dir_enabled()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_dpi_group())
        layout.addWidget(self._build_size_group())
        layout.addWidget(self._build_output_group())
        layout.addLayout(self._build_action_row())

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        layout.addWidget(QLabel("日志："))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("源目录（递归扫描其下所有 PDF）")
        row = QHBoxLayout(group)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("选择要扫描的目录……")
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._choose_source_dir)
        row.addWidget(self.source_edit, stretch=1)
        row.addWidget(browse_btn)
        return group

    def _build_dpi_group(self) -> QGroupBox:
        group = QGroupBox("目标分辨率")
        row = QHBoxLayout(group)
        row.addWidget(QLabel("图片压缩目标 DPI："))
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(10, 1200)
        self.dpi_spin.setValue(150)
        self.dpi_spin.setSuffix(" dpi")
        row.addWidget(self.dpi_spin)
        row.addStretch(1)
        hint = QLabel("仅降低已超过目标 DPI 的图片分辨率，PDF 其它内容与结构保持不变。")
        hint.setWordWrap(True)
        row.addWidget(hint, stretch=2)
        return group

    def _build_size_group(self) -> QGroupBox:
        group = QGroupBox("文件大小阈值")
        row = QHBoxLayout(group)
        row.addWidget(QLabel("仅压缩大于："))
        self.min_size_spin = QSpinBox()
        # 内部按字节比较；UI 以 KB 输入，上限约 2GiB。
        self.min_size_spin.setRange(0, 2_097_152)
        self.min_size_spin.setValue(0)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setSpecialValueText("不限制")
        self.min_size_spin.setSingleStep(64)
        row.addWidget(self.min_size_spin)
        row.addStretch(1)
        hint = QLabel("单位为 KB。设为 0（不限制）时处理全部 PDF；大于 0 时仅压缩文件大小超过该值的 PDF。")
        hint.setWordWrap(True)
        row.addWidget(hint, stretch=2)
        return group

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("输出方式")
        layout = QVBoxLayout(group)

        self.radio_overwrite = QRadioButton("覆盖原文件")
        self.radio_copy = QRadioButton("复制（原路径下生成 “原文件名_cr.pdf”）")
        self.radio_output_dir = QRadioButton("生成到指定目录（保留原有子目录结构）")
        self.radio_overwrite.setChecked(True)

        self.output_group = QButtonGroup(self)
        for btn in (self.radio_overwrite, self.radio_copy, self.radio_output_dir):
            self.output_group.addButton(btn)
            layout.addWidget(btn)

        self.output_group.buttonToggled.connect(self._on_output_mode_changed)

        dir_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("选择压缩结果输出目录……")
        self.output_dir_browse_btn = QPushButton("浏览…")
        self.output_dir_browse_btn.clicked.connect(self._choose_output_dir)
        dir_row.addWidget(self.output_dir_edit, stretch=1)
        dir_row.addWidget(self.output_dir_browse_btn)
        layout.addLayout(dir_row)

        return group

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.start_btn = QPushButton("开始")
        self.start_btn.clicked.connect(self._on_start_clicked)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        return row

    # ------------------------------------------------------------------
    # 交互回调
    # ------------------------------------------------------------------
    def _choose_source_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择源目录")
        if directory:
            self.source_edit.setText(directory)

    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if directory:
            self.output_dir_edit.setText(directory)

    def _on_output_mode_changed(self, _button: QAbstractButton, _checked: bool) -> None:
        self._update_output_dir_enabled()

    def _update_output_dir_enabled(self) -> None:
        enabled = self.radio_output_dir.isChecked()
        self.output_dir_edit.setEnabled(enabled)
        self.output_dir_browse_btn.setEnabled(enabled)

    def _current_mode(self) -> OutputMode:
        if self.radio_overwrite.isChecked():
            return OutputMode.OVERWRITE
        if self.radio_copy.isChecked():
            return OutputMode.COPY_SUFFIX
        return OutputMode.OUTPUT_DIR

    def _on_start_clicked(self) -> None:
        source_dir = self.source_edit.text().strip()
        if not source_dir:
            QMessageBox.warning(self, "提示", "请先选择源目录")
            return
        source_path = Path(source_dir)
        if not source_path.is_dir():
            QMessageBox.warning(self, "提示", "源目录不存在，请重新选择")
            return

        mode = self._current_mode()
        output_dir: Optional[Path] = None
        if mode is OutputMode.OUTPUT_DIR:
            output_dir_text = self.output_dir_edit.text().strip()
            if not output_dir_text:
                QMessageBox.warning(self, "提示", "请选择输出目录")
                return
            output_dir = Path(output_dir_text)
            output_dir.mkdir(parents=True, exist_ok=True)

        if mode is OutputMode.OVERWRITE:
            confirm = QMessageBox.question(
                self,
                "确认覆盖",
                "该操作将直接覆盖原始 PDF 文件，且不可撤销，确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self._set_running_state(True)

        self._worker = CompressWorker(
            root_dir=source_path,
            target_dpi=self.dpi_spin.value(),
            mode=mode,
            output_dir=output_dir,
            min_file_size=self.min_size_spin.value() * 1024,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self.log_view.appendPlainText)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_cancel_clicked(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.log_view.appendPlainText("已请求取消，正在完成当前文件后停止……")
            self.cancel_btn.setEnabled(False)

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setValue(int(done / total * 100))

    def _on_finished(
        self,
        processed: int,
        compressed: int,
        skipped: int,
        errors: int,
        original_total: int,
        new_total: int,
        cancelled: bool,
    ) -> None:
        self._set_running_state(False)
        self.progress_bar.setValue(100 if not cancelled else self.progress_bar.value())

        saved = max(0, original_total - new_total)
        ratio = (saved / original_total * 100.0) if original_total else 0.0
        status = "已取消" if cancelled else "全部完成"
        summary = (
            f"—— {status} ——\n"
            f"共处理 {processed} 个文件：压缩 {compressed} 个，跳过 {skipped} 个，出错 {errors} 个。\n"
            f"总体积：{original_total} 字节 → {new_total} 字节（节省 {ratio:.1f}%）"
        )
        self.log_view.appendPlainText(summary)
        self._worker = None

    def _set_running_state(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.source_edit.setEnabled(not running)
        self.dpi_spin.setEnabled(not running)
        self.min_size_spin.setEnabled(not running)
        for btn in (self.radio_overwrite, self.radio_copy, self.radio_output_dir):
            btn.setEnabled(not running)
        if running:
            self.output_dir_edit.setEnabled(False)
            self.output_dir_browse_btn.setEnabled(False)
        else:
            self._update_output_dir_enabled()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)


def main() -> None:
    import sys

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
