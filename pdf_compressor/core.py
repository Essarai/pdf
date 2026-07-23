"""PDF 图片压缩核心逻辑（不依赖 PyQt，可独立测试/命令行调用）。

主要能力：
1. 递归扫描目录下所有 PDF 文件。
2. 对 PDF 中每一张嵌入图片，按其在页面上的显示尺寸计算“有效 DPI”，
   若超过用户指定的目标 DPI，则按比例缩小像素尺寸并重新编码，
   仅替换图片内容本身，不改动 PDF 的其它内容/结构。
3. 支持三种输出方式：覆盖原文件 / 同目录下 `_cr` 后缀复制 / 输出到指定目录（保留相对子目录结构）。
"""

from __future__ import annotations

import io
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image

try:
    _RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
except AttributeError:  # pragma: no cover - 兼容旧版 Pillow
    _RESAMPLE = Image.LANCZOS


class OutputMode(Enum):
    """输出方式。"""

    OVERWRITE = "overwrite"       # 覆盖原文件
    COPY_SUFFIX = "copy_suffix"   # 同路径下生成 “原文件名_cr.pdf”
    OUTPUT_DIR = "output_dir"     # 生成到指定目录（保留相对子目录结构）


COPY_SUFFIX = "_cr"

# 计算出的“所需 DPI”与目标 DPI 相比，允许的宽容误差；避免因四舍五入导致
# 本来已经达标的图片被反复压缩。
DPI_TOLERANCE = 1.05


@dataclass
class FileResult:
    """单个 PDF 文件的处理结果。"""

    input_path: Path
    output_path: Optional[Path] = None
    original_size: int = 0
    new_size: int = 0
    images_total: int = 0
    images_compressed: int = 0
    images_skipped: int = 0
    skipped: bool = False  # True 表示整个文件无需压缩（所有图片已达标或处理失败）
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_size - self.new_size)

    @property
    def saved_ratio(self) -> float:
        if not self.original_size:
            return 0.0
        return self.saved_bytes / self.original_size * 100.0


def find_pdfs(root_dir: Path) -> List[Path]:
    """递归查找 root_dir 下所有 PDF 文件（大小写不敏感）。"""

    root_dir = Path(root_dir)
    return sorted(
        p for p in root_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".pdf"
    )


def _effective_dpi(pixel_dim: float, bbox_dim_pt: float) -> float:
    """给定图片像素尺寸和其在页面上的显示尺寸（单位：pt，1pt = 1/72 inch），
    计算有效 DPI。"""

    if bbox_dim_pt <= 0:
        return 0.0
    return pixel_dim / (bbox_dim_pt / 72.0)


JPEG_QUALITY = 85


def _resize_image_bytes(raw: bytes, ext: str, new_w: int, new_h: int) -> Optional[bytes]:
    """用 Pillow 将图片字节缩放到 new_w x new_h，并选择压缩效果最好的编码方式。

    重要说明：PDF 内部对图片的存储方式与常见图片容器格式并不完全等价——
    例如来源是 FlateDecode/PNG 的截图类图片，若缩放后仍以 PNG 方式写回，
    PyMuPDF 保存时通常会将其还原为“原始像素 + Flate 压缩”，压缩效率远低于
    JPEG（DCTDecode）。因此这里会分别尝试 PNG（无损，适合图标/线稿等色彩
    很少的图片）与 JPEG（适合照片/截图等连续色调图片），取体积更小的一个，
    以确保“压缩分辨率”这一操作确实能让文件变小，而不会不降反升。

    返回 None 表示无法处理（不支持的格式/解码失败等），调用方应跳过该图片。
    """

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    new_w = max(1, int(new_w))
    new_h = max(1, int(new_h))

    try:
        resized = img.resize((new_w, new_h), _RESAMPLE)
    except Exception:
        return None

    has_alpha = resized.mode in ("RGBA", "LA", "PA") or "transparency" in resized.info

    candidates: List[bytes] = []

    # PNG 候选：无损，对图标/线稿/色彩数很少的图片通常更省空间。
    try:
        png_buf = io.BytesIO()
        resized.save(png_buf, format="PNG", optimize=True)
        candidates.append(png_buf.getvalue())
    except Exception:
        pass

    # JPEG 候选：有损但对照片/截图等连续色调图片压缩率通常远高于 PNG。
    # 含透明通道的图片不能用 JPEG（会丢失透明信息），跳过该候选。
    if not has_alpha:
        try:
            jpeg_img = resized if resized.mode in ("RGB", "L") else resized.convert("RGB")
            jpeg_buf = io.BytesIO()
            jpeg_img.save(jpeg_buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            candidates.append(jpeg_buf.getvalue())
        except Exception:
            pass

    if not candidates:
        return None

    return min(candidates, key=len)


def _collect_image_usage(doc: "fitz.Document") -> Tuple[Dict[int, List], Dict[int, int]]:
    """遍历全部页面，收集每个图片 xref 在文档中出现的所有显示 bbox，
    以及可用于调用 replace_image 的某一页面索引（同一 xref 只需替换一次，
    PyMuPDF 会全局生效）。"""

    xref_rects: Dict[int, List] = {}
    xref_page: Dict[int, int] = {}

    for page_index in range(doc.page_count):
        page = doc[page_index]
        for img in page.get_images(full=True):
            xref = img[0]
            if xref not in xref_page:
                xref_page[xref] = page_index
                xref_rects[xref] = []
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            xref_rects[xref].extend(rects)

    return xref_rects, xref_page


def _process_document(doc: "fitz.Document", target_dpi: float, notes: List[str]) -> Tuple[int, int, int]:
    """遍历文档所有图片并按目标 DPI 压缩。

    返回 (images_total, images_compressed, images_skipped)。
    """

    xref_rects, xref_page = _collect_image_usage(doc)

    images_total = len(xref_rects)
    images_compressed = 0
    images_skipped = 0

    for xref, rects in xref_rects.items():
        try:
            info = doc.extract_image(xref)
        except Exception as exc:
            notes.append(f"图片 xref={xref} 提取失败，已跳过：{exc}")
            images_skipped += 1
            continue

        if not info:
            images_skipped += 1
            continue

        # 含透明蒙版（SMask）的图片跳过处理，避免破坏透明效果，
        # 保证“其它参数不变”。
        if info.get("smask"):
            notes.append(f"图片 xref={xref} 含透明蒙版，为保持效果不变已跳过")
            images_skipped += 1
            continue

        width = info.get("width") or 0
        height = info.get("height") or 0
        if width <= 0 or height <= 0 or not rects:
            images_skipped += 1
            continue

        required_dpi = 0.0
        for rect in rects:
            required_dpi = max(
                required_dpi,
                _effective_dpi(width, rect.width),
                _effective_dpi(height, rect.height),
            )

        if required_dpi <= 0 or required_dpi <= target_dpi * DPI_TOLERANCE:
            # 已经达标（或无法判断显示尺寸），不做处理，避免放大画质。
            images_skipped += 1
            continue

        scale = target_dpi / required_dpi
        new_w = round(width * scale)
        new_h = round(height * scale)
        if new_w >= width and new_h >= height:
            images_skipped += 1
            continue

        new_bytes = _resize_image_bytes(info["image"], info.get("ext", ""), new_w, new_h)
        if new_bytes is None:
            notes.append(f"图片 xref={xref} 重编码失败，已跳过")
            images_skipped += 1
            continue

        # 只有确实变小才替换，避免因重新编码导致体积不降反升时仍强行替换。
        if len(new_bytes) >= len(info["image"]):
            notes.append(f"图片 xref={xref} 压缩后体积未减小，已跳过")
            images_skipped += 1
            continue

        try:
            page = doc[xref_page[xref]]
            page.replace_image(xref, stream=new_bytes)
            images_compressed += 1
        except Exception as exc:
            notes.append(f"图片 xref={xref} 替换失败，已跳过：{exc}")
            images_skipped += 1

    return images_total, images_compressed, images_skipped


def resolve_output_path(
    input_path: Path,
    mode: OutputMode,
    source_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Path:
    """根据输出方式计算目标文件路径。"""

    input_path = Path(input_path)

    if mode is OutputMode.OVERWRITE:
        return input_path

    if mode is OutputMode.COPY_SUFFIX:
        return input_path.with_name(f"{input_path.stem}{COPY_SUFFIX}{input_path.suffix}")

    if mode is OutputMode.OUTPUT_DIR:
        if output_dir is None:
            raise ValueError("输出目录模式下 output_dir 不能为空")
        output_dir = Path(output_dir)
        if source_root is not None:
            try:
                rel = input_path.resolve().relative_to(Path(source_root).resolve())
            except ValueError:
                rel = Path(input_path.name)
        else:
            rel = Path(input_path.name)
        return output_dir / rel

    raise ValueError(f"未知的输出模式: {mode}")


def compress_pdf(
    input_path: Path,
    target_dpi: float,
    mode: OutputMode,
    source_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> FileResult:
    """压缩单个 PDF 文件中的图片，并按指定方式保存。"""

    input_path = Path(input_path)
    result = FileResult(input_path=input_path)

    try:
        result.original_size = input_path.stat().st_size
    except OSError as exc:
        result.error = f"无法读取文件：{exc}"
        return result

    try:
        output_path = resolve_output_path(input_path, mode, source_root, output_dir)
    except ValueError as exc:
        result.error = str(exc)
        return result
    result.output_path = output_path

    try:
        doc = fitz.open(str(input_path))
    except Exception as exc:
        result.error = f"打开失败：{exc}"
        return result

    tmp_path: Optional[Path] = None
    try:
        images_total, images_compressed, images_skipped = _process_document(
            doc, target_dpi, result.notes
        )
        result.images_total = images_total
        result.images_compressed = images_compressed
        result.images_skipped = images_skipped

        if images_compressed == 0:
            # 没有任何图片被压缩：原文件保留不变；复制/输出目录模式下仍需要
            # 产生目标文件（原样复制），以符合用户对输出方式的预期。
            result.skipped = True
            result.new_size = result.original_size
            doc.close()
            if mode is not OutputMode.OVERWRITE and output_path != input_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(input_path, output_path)
            return result

        if mode is OutputMode.OVERWRITE:
            # 注意：不能用 doc.saveIncr() ——增量保存只是在文件末尾追加新内容，
            # 旧的（未压缩）图片数据仍会保留在文件中，体积不但不会减小反而会增大。
            # 因此改为完整重写到同目录下的临时文件，成功后原子替换原文件，
            # 这样既能真正缩小体积，也不会遗留半写坏文件。
            # deflate=True 只是让保存时对流对象使用 Flate 压缩这一“物理编码细节”，
            # 不会改变页面内容/结构等语义参数；实测如果不开启，被替换的图片会以
            # 未压缩的原始像素写入文件，导致体积不降反升，因此必须开启。
            tmp_path = input_path.with_name(f"{input_path.name}.tmp_compress")
            doc.save(str(tmp_path), garbage=1, deflate=True, clean=False)
            doc.close()
            tmp_path.replace(input_path)
            output_path = input_path
            result.output_path = output_path
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(output_path), garbage=1, deflate=True, clean=False)
            doc.close()
    except Exception as exc:
        result.error = f"处理/保存失败：{exc}"
        try:
            doc.close()
        except Exception:
            pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return result

    try:
        result.new_size = output_path.stat().st_size
    except OSError:
        result.new_size = 0

    return result


def compress_directory(
    root_dir: Path,
    target_dpi: float,
    mode: OutputMode,
    output_dir: Optional[Path] = None,
    progress_cb: Optional[Callable[[int, int, FileResult], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[FileResult]:
    """递归压缩 root_dir 下所有 PDF。

    progress_cb(done_count, total_count, result) 在每个文件处理完后被调用。
    should_cancel() 返回 True 时会中止后续文件的处理（已处理的文件不会回滚）。
    """

    root_dir = Path(root_dir)
    pdfs = find_pdfs(root_dir)
    total = len(pdfs)
    results: List[FileResult] = []

    for index, pdf_path in enumerate(pdfs, start=1):
        if should_cancel and should_cancel():
            break
        result = compress_pdf(
            pdf_path,
            target_dpi,
            mode,
            source_root=root_dir,
            output_dir=output_dir,
        )
        results.append(result)
        if progress_cb:
            progress_cb(index, total, result)

    return results
