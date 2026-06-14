# Build & Packaging Reference

> 本文档仅记录 Windows PyInstaller 打包参考。Linux/Windows 源码运行兼容验收不依赖本命令。

从 CLAUDE.md 抽出的打包发布命令。

## PyInstaller 打包 (Windows 目标)

```bash
pyinstaller --noconfirm --windowed --name OCRExtract \
  --add-data "models/PP-OCRv5_mobile_det;models/PP-OCRv5_mobile_det" \
  --add-data "models/PP-OCRv5_mobile_rec;models/PP-OCRv5_mobile_rec" \
  --add-data "models/PP-LCNet_x1_0_textline_ori;models/PP-LCNet_x1_0_textline_ori" \
  --add-data "models/PP-LCNet_x1_0_doc_ori;models/PP-LCNet_x1_0_doc_ori" \
  --add-data "src/ui/assets/icons;src/ui/assets/icons" \
  src/app.py
```
