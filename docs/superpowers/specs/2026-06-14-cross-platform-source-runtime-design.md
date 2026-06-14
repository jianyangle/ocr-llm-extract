# Linux/Windows 源码运行兼容设计

## 问题

项目当前在 WSL/Linux 环境开发，README 声明操作系统支持 `Windows / Linux / macOS`。但现有证据更偏向 Windows 首版和 WSL/Linux 开发路径：发布脚本默认 Windows `.exe`，PyInstaller 命令使用 Windows `--add-data` 分隔符；运行时代码大多使用 `pathlib`，但缺少 Linux/Windows 源码运行的明确验收矩阵。

本设计只解决源码运行兼容：Linux 和 Windows 用户在仓库根目录创建虚拟环境、安装依赖后，能运行 `python src/app.py` 并完成基本 OCR/抽取/Excel 写入流程。

## 目标

1. 明确支持范围：本阶段目标是 Linux/Windows 源码运行兼容；macOS 未验证；PyInstaller 打包产物不在本阶段范围内。
2. 保持运行时代码改动小：只修直接影响源码运行兼容的问题，不引入平台抽象层。
3. 补齐验证材料：把 Linux/Windows 的安装、启动、配置、OCR/PDF、Excel 写入和 LLM 连接提示纳入 checklist。
4. 避免误导：不能把 WSL/Linux 测试结果当作 Windows 原生环境通过。

## 非目标

1. 不做 Windows 或 Linux 的 PyInstaller 打包改造。
2. 不承诺 macOS 支持。
3. 不迁移现有配置目录到 Windows `AppData`。
4. 不新增自动下载模型、自动修复依赖、自动替换 Ollama 地址等行为。
5. 不引入 `src/platform`、`src/runtime` 之类的大平台抽象层。

## 设计

### 1. 支持范围和文档

**涉及文件**：`README.md`、`docs/claude/build-packaging.md`、新增源码运行兼容 checklist。

README 的操作系统说明改为：

- Linux/Windows：本阶段源码运行兼容目标。
- macOS：未验证，不作为本阶段承诺。
- Windows 打包命令：保留现有说明，但明确它属于 Windows 打包参考，不属于源码运行兼容验收。

安装说明拆成两段：

| 环境 | 创建虚拟环境 | 激活虚拟环境 |
|------|--------------|--------------|
| Linux/WSL | `python3.12 -m venv .venv` | `source .venv/bin/activate` |
| Windows PowerShell | `py -3.12 -m venv .venv` | `.venv\Scripts\Activate.ps1` |

文档明确要求从仓库根目录启动：

```bash
python src/app.py
```

这样可以保证 `Path.cwd()` 指向项目根目录，本地 OCR 模型从 `models/` 读取。

### 2. 配置和日志目录

**涉及文件**：`src/io/config_store.py`、`src/io/log_store.py`、`src/ui/settings_dialog.py`。

本阶段保留现有目录：

- 配置：`Path.home() / ".ocr_extract_app" / "config.json"`
- 日志：`Path.home() / ".ocr_extract_app" / "logs"`

保留原因：

1. 该目录在 Linux 和 Windows 都可用。
2. 改到 Windows `AppData` 会引入旧配置迁移、兼容策略和用户排查成本。
3. 源码运行兼容的核心目标是“可运行、可验证”，不是平台原生目录体验。

需要修改的是用户可见文案。设置页当前提示 `~/.ocr_extract_app/config.json`，对 Windows 用户不直观。文案改为“配置文件位于用户主目录下的 `.ocr_extract_app/config.json`”。这不改变路径，只减少误解。

### 3. 资源和模型路径

**涉及文件**：`src/app.py`、`src/ui/icon_loader.py`、`src/ui/theme.py`、`src/ocr/paddle_service.py`。

源码运行路径保持现状：

- 字体来自 `data/fonts`。
- 应用图标来自 `src/ui/assets/icons/app_icon`。
- 通用图标来自 `src/ui/assets/icons`。
- 本地 OCR 模型来自 `<project_root>/models`。

这些路径已使用 `Path(__file__).resolve()` 或 `Path.cwd()` 组合，跨平台基础良好。本阶段不改资源结构，也不新增模型下载逻辑。若 `models/` 缺失，继续使用现有 OCR 初始化错误暴露问题。

### 4. Excel 输出路径和文件名

**涉及文件**：`src/io/excel_writer.py`、`tests/test_io_excel_writer.py`。

`ExcelWriter` 已清理 Windows 和 Linux 都不安全的文件名字符：`<>:"/\|?*` 和控制字符。继续保留该策略。

本阶段补测试，不假装在 Linux 上完整模拟 Windows 文件系统：

- 覆盖 Windows 非法字符会被清理。
- 覆盖尾随点和空格会被清理。
- 覆盖空文件名会回退到 `example.xlsx` 或 `export.xlsx`。
- 不用 `Path("C:\\foo\\bar.xlsx")` 在 Linux 上断言 Windows 路径行为，因为 Linux 会把它当成普通文件名，这会制造错误信心。

### 5. LLM/Ollama 平台提示

**涉及文件**：`src/extract/network_diagnostics.py`、`src/extract/connection_check.py`、`src/extract/model_fetcher.py`。

现有代码已经按 Windows、Linux、WSL 区分 Ollama localhost 超时提示。本阶段保留行为，并把它纳入源码运行兼容 checklist：

- Windows 原生 + `localhost`：提示确认 Ollama 已启动并监听 `localhost:11434`。
- Linux 原生 + `localhost`：提示确认本机 Ollama 服务已启动。
- WSL + `localhost`：提示 `localhost` 指向 WSL 环境；如果 Ollama 跑在 Windows，用户需要填写 Windows 主机 IP 或让 Ollama 监听 `0.0.0.0`。

本阶段不自动探测 Windows 主机 IP，也不自动改写用户输入的地址。

### 6. 错误处理策略

本阶段的原则是失败可解释，不做自动修复。

- 依赖安装失败：通过 README 和 checklist 提醒 `paddlepaddle`、`paddleocr`、`PySide6` 是平台相关依赖，需要在 Windows/Linux fresh venv 中分别验证。
- 启动失败：继续使用现有异常链和错误码，不新增静默 fallback。
- 配置保存失败：保留现有 `ConfigStore.save()` 行为，让文件系统错误显式暴露。
- OCR 模型缺失：继续由 `PaddleOCRService` 初始化校验暴露。

## 测试和验收

### 1. 自动化测试

实现后至少运行：

```bash
.venv/bin/python -m pytest tests/test_io_excel_writer.py tests/test_io_config_store.py tests/test_connection_check.py tests/test_model_fetcher.py tests/test_ui_font_assets.py tests/test_app_composition.py
```

新增或补充：

- Excel 文件名清理测试。
- 设置页配置路径提示测试。
- 文档/checklist 中的平台约束对应自动测试或人工验收项。

### 2. Linux/WSL smoke

在当前 Linux/WSL 环境执行：

```bash
.venv/bin/python -m pytest tests/test_io_excel_writer.py tests/test_io_config_store.py tests/test_connection_check.py tests/test_model_fetcher.py tests/test_ui_font_assets.py tests/test_app_composition.py
```

如需 GUI smoke，沿用项目现有 Qt 测试方式，不新增测试框架。

### 3. Windows 人工 smoke checklist

在 Windows PowerShell 中从干净环境验证：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python src/app.py
```

应用内验收：

1. 能打开设置并保存配置。
2. 能导入文本并跑抽取流程。
3. 能导入图片并触发本地 OCR。
4. 能导入 PDF 并触发 PDF 处理。
5. 能写出 `.xlsx`。
6. Ollama localhost 超时时显示 Windows 版本提示，不出现 WSL 主机 IP 误导。

## 改动范围

| 文件 | 改动 |
|------|------|
| `README.md` | 明确 Linux/Windows 源码运行目标，macOS 未验证；拆分安装命令；说明从仓库根目录启动 |
| `docs/claude/build-packaging.md` | 标注现有命令是 Windows 打包参考，不属于本阶段源码运行验收 |
| `src/ui/settings_dialog.py` | 调整配置路径提示文案 |
| `tests/test_ui_qt_settings_dialog.py` | 更新配置路径提示断言 |
| `tests/test_io_excel_writer.py` | 增补文件名清理测试 |
| `docs/superpowers/specs/2026-06-14-cross-platform-source-runtime-design.md` | 记录本设计 |
| 新增源码运行兼容 checklist | 记录 Linux/Windows 自动和人工 smoke 步骤 |

## 风险

1. Windows 原生环境仍需实测；WSL 通过不能代表 Windows 通过。
2. `paddlepaddle`、`paddleocr`、`pypdfium2` 是平台相关依赖，安装失败可能来自上游 wheel 或 Python 版本组合。
3. 保留 `~/.ocr_extract_app` 等价目录虽然可运行，但不是 Windows 原生体验；后续若面向普通 Windows 用户发布安装包，应单独设计配置目录迁移。
4. 本阶段不改 PyInstaller，最终用户可执行包仍可能存在资源路径或打包参数问题。

## 后续

本设计批准并落盘后，下一步用 `writing-plans` 编写实施计划。计划应保持顺序：先文档和测试，再做少量文案/测试代码修改，最后运行 Linux/WSL 验证，并把 Windows smoke 标为待人工执行。
