# PDF 图片压缩工具

一个基于 **PyQt6** 的桌面小工具：递归扫描指定目录下的所有 PDF 文件，
将其中嵌入的图片按指定 DPI 降采样压缩，**PDF 的其它内容/结构保持不变**
（不改变文字、字体、页面尺寸、图层结构等）。

## 功能

- 递归扫描目录下所有 `.pdf` 文件。
- 按目标 DPI 计算每张图片的“有效分辨率”（图片像素尺寸 ÷ 页面显示尺寸），
  只压缩超过目标 DPI 的图片，不会放大画质。
- 自动在 PNG（无损，适合图标/线稿）与 JPEG（适合照片/截图）两种编码中
  选择压缩后体积更小的一种；含透明通道的图片会自动跳过，以保证效果不变。
- 三种输出方式：
  1. **覆盖原文件**
  2. **复制**：在原路径下生成 `原文件名_cr.pdf`
  3. **生成到指定目录**：保留相对于源目录的子目录结构

## 目录结构

```
pdf/
  main.py                 # 入口，启动 GUI
  requirements.txt
  pdf_compressor/
    core.py               # 核心压缩逻辑（不依赖 Qt）
    worker.py             # QThread 后台任务
    gui.py                # PyQt6 界面
```

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Windows EXE（GitHub Actions）

推送到 `main` 或手动触发 Actions 后，会在 **Actions → Build Windows EXE** 产物中上传
`PDFImageCompressor.exe`（PyInstaller onefile + windowed）。

打 tag 发布示例：

```bash
git tag v0.1.0
git push origin v0.1.0
```

tag 触发时会额外创建 GitHub Release，并附带该 exe。

本地自行打包（可选）：

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --onefile \
  --name PDFImageCompressor \
  --collect-all PyQt6 --collect-all pymupdf --collect-all PIL \
  main.py
```

## 使用说明

1. 选择要扫描的源目录（会递归查找其下所有 PDF）。
2. 设置目标 DPI（默认 150）。
3. 选择输出方式（覆盖 / 复制 / 指定目录）。
4. 点击“开始”，日志区会实时显示每个文件的处理结果与压缩比例。

## 实现细节 / 已知限制

- 使用 [PyMuPDF](https://pymupdf.readthedocs.io/) 的 `Page.replace_image()`
  做“全局”图片替换（同一图片在多处引用会一起更新），并以完整重写
  （非增量）的方式保存，因为增量保存只会在文件末尾追加内容，旧的图片数据
  仍留在文件中，无法真正减小体积。
- 保存时使用 `garbage=1, deflate=True, clean=False`：只清理彻底无引用的
  对象、对流对象使用 Flate 压缩，不改变页面内容/结构等语义参数。
- 含透明蒙版（`/SMask`）的图片会被跳过，避免破坏透明效果。
- 少数使用特殊编码（如 CCITTFax、JBIG2、JPX）且 Pillow 无法解码的图片会被
  跳过并在日志中给出提示，不会中断整体处理。
