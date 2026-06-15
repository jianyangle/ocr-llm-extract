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
- `--collect-all paddle/paddleocr/paddlex`：收集 Paddle 系列的原生库、数据文件与隐藏依赖，缺失会导致冻结包运行 OCR 时崩溃。
- `data/fonts`、`data/icons`、`data/icon_1rfurz1zeyz`：运行时从 `_MEIPASS/data/...` 读取的字体与工具栏/任务队列图标，缺失会导致字体回退、图标空白。
- `src/ui/assets/icons`：SVG 控件图标 + 多尺寸 app_icon PNG（窗口图标）。
- `src/ui/assets/providers`：设置面板按 provider 加载的 LLM logo（`settings_dialog.py` 运行时读取），缺失会导致各 provider logo 空白。
- `models/UVDoc` 不打包（默认不启用 doc unwarping）。
- `--icon src/ui/assets/icons/app_icon/OLE.ico`：exe 自身图标（资源管理器/任务栏）。`OLE.ico` 内嵌 16~256 多档尺寸，避免缩放降采样发糊。该文件由 `OLE_256.png` 经 Pillow 生成：
  ```bash
  python -c "from PIL import Image; Image.open('src/ui/assets/icons/app_icon/OLE_256.png').save('src/ui/assets/icons/app_icon/OLE.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
  ```
