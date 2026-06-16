# 启动加速：本地 OCR 模型后台预热设计

- 日期：2026-06-16
- 状态：待评审
- 关联：`src/app.py`、`src/ocr/paddle_service.py`、`src/ocr/routing_service.py`、`src/ui/main_window.py`

## 背景与根因（实测）

打包后「双击 exe → 主窗口出现」耗时约 **7.25s**（热启动 6.58~7.74s，n=5；冷启动更慢），体验明显迟滞。

分段实测定位（源码态探针）：

| 环节 | 实测 | 结论 |
|------|------|------|
| 全部启动 import 之和 | 0.74s | 非主因（PySide6 0.10s / openpyxl 0.42s 等） |
| `build_main_window()` | 8.11s，期间打印 `Creating model: PP-OCRv5_mobile_det/rec/...` | **主因** |

根因链：

- `src/app.py:137-138`：`window = build_main_window()` 在 `window.show()` **之前**同步执行。
- `src/app.py:103-112`：`build_main_window` 构造 `PaddleOCRService(...)`。
- `src/ocr/paddle_service.py:93`：`__init__` 直接 `self._engine = self._create_engine(...)` → 触发 `from paddleocr import PaddleOCR` + 加载 4 个 OCR 模型，阻塞窗口显示。

UPX、Windows Defender、PySide6 import 均被 0.74s vs 8.11s 的悬殊证伪为次要，本设计不处理。

## 目标 / 非目标

### 目标
- 窗口秒开：`build_main_window()` 不再加载模型，启动到出窗口 < 1.5s。
- 进界面后**仅当 `ocr_use_online == False`** 时，后台工作线程静默预热本地模型。
- 状态栏提供独立的「本地模型加载中」轻量提示，加载完消失、失败转错误态。
- 线程安全：预热与用户抢先点识别不发生双重加载或竞态。

### 非目标
- 不优化 UPX / Defender / import（已证伪为次要）。
- 不改在线 OCR 路径。
- 不引入模型加载进度条（仅"加载中/就绪/失败"三态）。

## 设计

### A. `paddle_service.py`：懒加载 + 线程安全预热

- `__init__`：删除 `self._engine = self._create_engine(self._runtime_options)`（line 93），改为 `self._engine = None`。
- 新增私有 `_ensure_engine_locked()`：`if self._engine is None: self._engine = self._create_engine(self._runtime_options)`。**调用方必须已持有 `self._recognize_lock`**。
- 新增公有 `preload()`：`with self._recognize_lock: self._ensure_engine_locked()`。幂等；引擎已建则瞬时返回。
- `_recognize_locked()`（line 169）开头调用 `_ensure_engine_locked()` 兜底懒建（覆盖"在线切本地后首次识别"路径）。
- `_reset_runtime_locked()`（line 104-107）：将 `self._engine = self._create_engine(...)` 改为 `self._engine = None`（惰性重建）。同源收益：消除「设置保存」在 GUI 线程同步重建引擎的卡顿（`main_window.py:1828` 路径）。

> 线程安全依据：`recognize()`（line 166）与 `preload()` 均通过同一把 `self._recognize_lock` 串行化。预热持锁加载期间用户点识别，识别线程在锁上阻塞等待，加载完成后复用同一引擎，无双重加载、无竞态。

### B. `routing_service.py`：条件预热入口

- 新增 `maybe_preload_local()`：`if not self._use_online: self._local.preload()`。预热的「仅本地」决策只在此一处。

### C. `app.py`：先显示后预热

- `main()` 顺序：`window = build_main_window()`（已变快）→ `window.show()` → 触发 MainWindow 的后台预热（不阻塞 `app.exec()`）。
- 预热的 QThread 生命周期由 MainWindow 持有（见 D），`main()` 不直接管理线程。

### D. `main_window.py`：后台 worker + 独立指示器

- **独立指示器**：在 `status_bar_layout`（line 1088）右侧新增一个独立 `QLabel`（状态点 + 文字「本地模型加载中…」），默认 `hidden`，与 `_set_status` 的主状态文字解耦，互不覆盖。
- **PreloadWorker(QObject)**：`run()` 调 `ocr_service.maybe_preload_local()`；成功发 `finished`，异常发 `failed(str)`（捕获 `OCRServiceError`/通用异常，绝不让后台线程崩溃进程）。
- **触发**：窗口显示后启动一次（`QTimer.singleShot(0, ...)` 或 `showEvent` 首次），复用现有 `QThread + moveToThread` 范式（参照识别 worker，line 1627-1629）。
  - 启动即显示指示器（脉冲态）；`finished` → 隐藏指示器；`failed` → 指示器转红 +「本地模型加载失败」。
- **关闭清理**：参照 `main_window.py:1649` 识别线程清理逻辑，关闭时若预热线程在跑，请求退出并 `wait()`，避免 QThread 析构告警。

## 数据流

```
启动: main() -> build_main_window()[不加载模型, ~0.7s] -> window.show()[秒开]
         -> QTimer.singleShot(0) -> PreloadWorker(QThread)
              -> ocr_service.maybe_preload_local()
                   -> if not use_online: PaddleOCRService.preload()
                        -> with lock: _ensure_engine_locked() -> _create_engine()[~8s, 后台]
              -> finished/failed -> 主线程信号 -> 更新独立指示器

识别: recognize() -> with lock -> _recognize_locked() -> _ensure_engine_locked()[已预热则瞬时, 否则懒建]
```

## 错误处理

- 后台预热失败：`PreloadWorker` 捕获异常 → `failed(message)` → 指示器转错误态；进程不崩溃。
- 预热失败后用户点识别：`recognize()` 走 `_ensure_engine_locked()` 重新尝试 `_create_engine()`，沿用既有 `E_OCR_002` 错误码上抛 UI（与当前行为一致）。
- 在线模式（`ocr_use_online == True`）：`maybe_preload_local()` 直接跳过，指示器不显示。

## 测试

- `paddle_service`：
  - `preload()` 后 `_engine` 非空；重复 `preload()` 幂等（`_create_engine` 仅调用一次，用计数 stub 工厂验证）。
  - `__init__` 不再调用 `_create_engine`（注入 stub 工厂，断言构造期零调用）——表达"构造不加载"的业务约束。
  - 并发：一个线程 `preload()` 持锁期间，另一线程 `recognize()` 阻塞至加载完成且只加载一次（stub 工厂计数 == 1）。
  - `_reset_runtime_locked` 后 `_engine` 置空，下次 `recognize`/`preload` 重建。
- `routing_service`：
  - `maybe_preload_local()` 在 `use_online=False` 调用 `local.preload()`；`use_online=True` 不调用（mock local）。
- `main_window`（可注入 stub ocr_service）：预热成功/失败时指示器可见性与状态切换正确。

## 风险

- 预热与识别共用一把锁：极端下"刚进界面立刻点识别"会等满模型加载（约 8s），与现状同量级，且有可见加载提示，可接受。
- `_reset_runtime_locked` 改惰性重建属同源附带修复，若需严格最小范围可剔除该条（reset 保持同步重建），不影响启动加速主目标。
