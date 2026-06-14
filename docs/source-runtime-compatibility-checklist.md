# Linux/Windows 源码运行兼容 Checklist

## 范围

本 checklist 只验证源码运行兼容：在仓库根目录创建虚拟环境、安装依赖并运行 `python src/app.py`。它不验证 PyInstaller 打包产物，也不承诺 macOS。

## Linux / WSL 自动验证

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
.venv/bin/python -m pytest tests/test_io_excel_writer.py tests/test_io_config_store.py tests/test_connection_check.py tests/test_model_fetcher.py tests/test_ui_font_assets.py tests/test_ui_qt_settings_dialog.py tests/test_app_composition.py
python src/app.py
```

验收：

1. 应用窗口能启动。
2. 设置页能打开，保存配置后用户主目录下出现 `.ocr_extract_app/config.json`。
3. 能写出 `.xlsx`。
4. Ollama localhost 超时时，WSL 环境提示 Windows 主机 IP；Linux 原生环境提示本机 Ollama 服务。

## Windows PowerShell 人工验证

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest tests/test_io_excel_writer.py tests/test_io_config_store.py tests/test_connection_check.py tests/test_model_fetcher.py tests/test_ui_font_assets.py tests/test_ui_qt_settings_dialog.py tests/test_app_composition.py
python src/app.py
```

验收：

1. 应用窗口能启动。
2. 设置页能打开，保存配置后用户主目录下出现 `.ocr_extract_app\config.json`。
3. 能导入文本任务。若要跑完整抽取流程，必须先配置可用的 OpenAI 兼容 API 或本机 Ollama；没有可用 LLM 时，只验证连接失败提示，不把外部服务缺失判为源码运行失败。
4. 能导入图片并触发本地 OCR。
5. 能导入 PDF 并触发 PDF 处理。
6. 能写出 `.xlsx`。
7. Ollama localhost 超时时显示 Windows 版本提示，不出现 WSL 主机 IP 误导。

## 失败归因

- `paddlepaddle`、`paddleocr`、`pypdfium2` 安装失败时，先按平台依赖问题处理。
- `models/` 缺失导致 OCR 初始化失败时，先确认仓库模型文件完整。
- 远程 LLM 或本机 Ollama 不可用时，只影响抽取连接，不代表源码运行失败。
