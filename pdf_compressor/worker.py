"""在后台线程中运行 PDF 压缩任务，避免阻塞 PyQt 主界面。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from .core import FileResult, OutputMode, compress_directory, find_pdfs


class CompressWorker(QThread):
    """执行“递归扫描 + 压缩”任务的工作线程。"""

    # (done_count, total_count)
    progress = pyqtSignal(int, int)
    # 单个文件处理完成后的日志文本
    log = pyqtSignal(str)
    # 全部完成：(处理文件数, 压缩文件数, 跳过文件数, 出错文件数, 原总大小, 新总大小, 是否被取消)
    finished_all = pyqtSignal(int, int, int, int, int, int, bool)

    def __init__(
        self,
        root_dir: Path,
        target_dpi: float,
        mode: OutputMode,
        output_dir: Optional[Path] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.root_dir = Path(root_dir)
        self.target_dpi = target_dpi
        self.mode = mode
        self.output_dir = Path(output_dir) if output_dir else None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _should_cancel(self) -> bool:
        return self._cancelled

    def _on_progress(self, done: int, total: int, result: FileResult) -> None:
        self.progress.emit(done, total)
        self.log.emit(self._format_result(done, total, result))

    @staticmethod
    def _format_result(done: int, total: int, result: FileResult) -> str:
        prefix = f"[{done}/{total}] {result.input_path.name}"
        if result.error:
            return f"{prefix} —— 错误：{result.error}"
        if result.skipped:
            return f"{prefix} —— 无需压缩（{result.images_total} 张图片均已达标或不支持处理）"
        return (
            f"{prefix} —— {result.images_compressed}/{result.images_total} 张图片已压缩，"
            f"{_fmt_size(result.original_size)} → {_fmt_size(result.new_size)} "
            f"（节省 {result.saved_ratio:.1f}%）"
        )

    def run(self) -> None:  # noqa: D102 (QThread override)
        try:
            pdfs = find_pdfs(self.root_dir)
        except Exception as exc:
            self.log.emit(f"扫描目录失败：{exc}")
            self.finished_all.emit(0, 0, 0, 0, 0, 0, self._cancelled)
            return

        total = len(pdfs)
        self.log.emit(f"共找到 {total} 个 PDF 文件，目标 DPI = {self.target_dpi}")

        results = compress_directory(
            self.root_dir,
            self.target_dpi,
            self.mode,
            output_dir=self.output_dir,
            progress_cb=self._on_progress,
            should_cancel=self._should_cancel,
        )

        processed = len(results)
        compressed = sum(1 for r in results if not r.error and not r.skipped)
        skipped = sum(1 for r in results if not r.error and r.skipped)
        errors = sum(1 for r in results if r.error)
        original_total = sum(r.original_size for r in results)
        new_total = sum(r.new_size for r in results)

        self.finished_all.emit(
            processed, compressed, skipped, errors, original_total, new_total, self._cancelled
        )


def _fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
