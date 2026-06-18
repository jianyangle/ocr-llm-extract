# Build & Packaging Reference

> 本文档仅记录 Windows PyInstaller 打包参考。Linux/Windows 源码运行兼容验收不依赖本命令。

从 CLAUDE.md 抽出的打包发布命令。

## PyInstaller 打包 (Windows 目标)

```bash
pyinstaller --noconfirm --windowed --name OCRExtract \
  --icon "src/ui/assets/icons/app_icon/OLE.ico" \
  --collect-all paddle \
  --collect-all paddleocr \
  --collect-all paddlex \
  --copy-metadata paddlex \
  --copy-metadata paddleocr \
  --copy-metadata imagesize \
  --copy-metadata opencv-contrib-python \
  --copy-metadata pyclipper \
  --copy-metadata pypdfium2 \
  --copy-metadata shapely \
  --add-data "models/PP-OCRv5_mobile_det;models/PP-OCRv5_mobile_det" \
  --add-data "models/PP-OCRv5_mobile_rec;models/PP-OCRv5_mobile_rec" \
  --add-data "models/PP-LCNet_x1_0_textline_ori;models/PP-LCNet_x1_0_textline_ori" \
  --add-data "models/PP-LCNet_x1_0_doc_ori;models/PP-LCNet_x1_0_doc_ori" \
  --add-data "data/fonts;data/fonts" \
  --add-data "data/icons;data/icons" \
  --add-data "data/icon_1rfurz1zeyz;data/icon_1rfurz1zeyz" \
  --add-data "src/ui/assets/icons;src/ui/assets/icons" \
  --add-data "src/ui/assets/providers;src/ui/assets/providers" \
  src/app.py
```

说明：
- PyInstaller 版本要求 **≥ 6.14**（numpy hook 自 6.14.1 起兼容 numpy 2.3+）。低于此版本 + numpy 2.4 会在导入期报 `ImportError: cannot load module more than once per process`（`numpy._core._multiarray_umath` 被重复加载）。本项目实测使用 6.21.0。
- `--collect-all paddle/paddleocr/paddlex`：收集 Paddle 系列的原生库、数据文件与隐藏依赖，缺失会导致冻结包运行 OCR 时崩溃。
- `--copy-metadata paddlex/paddleocr/imagesize/opencv-contrib-python/pyclipper/pypdfium2/shapely`：PaddleX 在创建 OCR pipeline 时通过 `importlib.metadata` 校验 `ocr-core` 依赖（`require_extra("ocr", alt="ocr-core")`）。冻结包默认不含这些依赖的 `.dist-info` 元数据，会使 `importlib.metadata.version()` 返回 `None` 并抛 `DependencyError: \`OCR\` requires additional dependencies`（被包成 `E_OCR_002: Failed to load PaddleOCR models`）。`ocr-core` 依赖 = `imagesize / opencv-contrib-python / pyclipper / pypdfium2 / shapely`。
- `data/fonts`、`data/icons`、`data/icon_1rfurz1zeyz`：运行时从 `_MEIPASS/data/...` 读取的字体与工具栏/任务队列图标，缺失会导致字体回退、图标空白。
- `src/ui/assets/icons`：SVG 控件图标 + 多尺寸 app_icon PNG（窗口图标）。
- `src/ui/assets/providers`：设置面板按 provider 加载的 LLM logo（`settings_dialog.py` 运行时读取），缺失会导致各 provider logo 空白。
- `models/UVDoc` 不打包（默认不启用 doc unwarping）。
- `--icon src/ui/assets/icons/app_icon/OLE.ico`：exe 自身图标（资源管理器/任务栏）。`OLE.ico` 内嵌 16~256 多档尺寸，避免缩放降采样发糊。该文件由 `OLE_256.png` 经 Pillow 生成：
  ```bash
  python -c "from PIL import Image; Image.open('src/ui/assets/icons/app_icon/OLE_256.png').save('src/ui/assets/icons/app_icon/OLE.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
  ```
- **任务栏图标**：主窗口无边框（`FramelessWindowHint`），窗口图标只在 Alt+Tab 和任务栏体现，而任务栏按钮的图标归属由进程 AppUserModelID 决定。`src/app.py` 的 `_set_windows_app_id()` 在 `QApplication` 创建前设置该 id（否则任务栏回退到空白/通用图标）。**打包后需人工验收**：运行 `dist/OCRExtract/OCRExtract.exe`，确认任务栏按钮显示 OLE 图标而非空白。若仍空白，再排查 `_app_icon()` 是否真正加载到 `OLE_*.png`。
