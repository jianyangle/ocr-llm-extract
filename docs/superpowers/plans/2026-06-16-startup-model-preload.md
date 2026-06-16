# 本地 OCR 模型后台预热 Implementation Plan

> **致执行代理：** 必需子技能（REQUIRED SUB-SKILL）：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐步实现本计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**Goal:** 让打包后「双击 exe → 主窗口出现」从 ~7.25s 降到秒级——把 PaddleOCR 引擎从「构造即加载」改为「懒加载 + 进界面后台预热」，并在状态栏加独立「本地模型加载中」提示。

**Architecture:** `PaddleOCRService.__init__` 不再同步建引擎，改由公有 `preload()` 与识别路径上的 `_ensure_engine_locked()` 在持锁状态下懒建（同一把 `_recognize_lock` 串行化，无双重加载）。`RoutingOCRService.maybe_preload_local()` 仅在 `ocr_use_online == False` 时触发本地预热。`app.py` 在 `window.show()` 后调 `MainWindow.start_model_preload()`，用后台 `QThread` worker 执行预热，状态栏独立指示器反映加载中/就绪/失败。

**Tech Stack:** Python 3.12、PySide6（QThread + moveToThread + Signal）、pytest（offscreen Qt）、PaddleOCR（懒加载）。

**对照 spec:** `docs/superpowers/specs/2026-06-16-startup-model-preload-design.md`

---

## File Structure

| 文件 | 职责 | 改动 |
|------|------|------|
| `src/ocr/paddle_service.py` | 本地 OCR 引擎封装 | `__init__` 改懒加载；新增 `preload()` / `_ensure_engine_locked()`；`_recognize_locked` 兜底懒建；`_reset_runtime_locked` 改置空 |
| `src/ocr/routing_service.py` | 本地/在线路由 | 新增 `maybe_preload_local()`（仅本地时调 `local.preload()`） |
| `src/ui/main_window.py` | 主窗口 | 新增 `_ModelPreloadWorker`、状态栏独立指示器、`start_model_preload()` + 完成/失败槽 + 关闭清理 |
| `src/app.py` | 启动入口 | `window.show()` 后触发 `start_model_preload()` |
| `tests/test_ocr_paddle_service.py` | 既有测试 | 迁移 2 个依赖「构造即加载」的用例；新增懒加载/预热/并发用例 |
| `tests/test_ocr_routing_service.py` | 既有测试 | `_RecordingService` 加 `preload`；新增条件预热用例 |
| `tests/test_ui_model_preload.py` | 新建 | `_ModelPreloadWorker` 同步单测（finished/failed） |

---

### Task 1: `PaddleOCRService` 懒加载 + 后台预热入口

**Files:**
- Modify: `src/ocr/paddle_service.py:88-94`（`__init__`）、`104-107`（`_reset_runtime_locked`）、`159-178`（`recognize` / `_recognize_locked` 起始）
- Modify: `tests/test_ocr_paddle_service.py:50-81`（迁移 2 个用例）
- Test: `tests/test_ocr_paddle_service.py`（新增用例）

- [ ] **Step 1: 写失败测试 —— 构造不加载引擎 / preload 幂等 / reset 置空 / 懒建**

在 `tests/test_ocr_paddle_service.py` 末尾追加（顶部已 `from pathlib import Path`、`import pytest`、`from src.ocr.paddle_service import PaddleOCRService`、`from src.ocr.errors import OCRServiceError`、`MODEL_DIRS` 与 `_create_model_tree` 均已存在）：

```python
def _counting_factory():
    """返回 (factory, counter)：factory 每次被调用计数 +1，产出一个空结果引擎。"""
    counter = {"n": 0}

    def _factory(**_kwargs):
        counter["n"] += 1

        def _engine(_image_path: str):
            return []

        return _engine

    return _factory, counter


def test_construction_does_not_build_engine(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    factory, counter = _counting_factory()

    PaddleOCRService(models_root=models_root, engine_factory=factory)

    assert counter["n"] == 0  # 构造期零加载：窗口才能秒开


def test_preload_builds_engine_once_and_is_idempotent(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    factory, counter = _counting_factory()
    service = PaddleOCRService(models_root=models_root, engine_factory=factory)

    service.preload()
    service.preload()

    assert counter["n"] == 1


def test_recognize_builds_engine_lazily_without_preload(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png")
    factory, counter = _counting_factory()
    service = PaddleOCRService(models_root=models_root, engine_factory=factory)

    service.recognize(str(image_path))

    assert counter["n"] == 1


def test_preload_then_recognize_reuses_single_engine(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png")
    factory, counter = _counting_factory()
    service = PaddleOCRService(models_root=models_root, engine_factory=factory)

    service.preload()
    service.recognize(str(image_path))

    assert counter["n"] == 1  # 预热后识别复用同一引擎，无二次加载


def test_reset_runtime_clears_engine_for_lazy_rebuild(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png")
    factory, counter = _counting_factory()
    service = PaddleOCRService(models_root=models_root, engine_factory=factory)

    service.preload()
    assert counter["n"] == 1
    service.reset_runtime()
    service.recognize(str(image_path))

    assert counter["n"] == 2  # reset 后引擎置空，下次识别重建


def test_concurrent_preload_and_recognize_build_engine_once(tmp_path: Path) -> None:
    """并发约束：preload 持锁加载期间，recognize 阻塞至加载完成，且引擎只建一次。"""
    import threading

    models_root = _create_model_tree(tmp_path / "models")
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png")

    counter = {"n": 0}
    factory_entered = threading.Event()
    release_factory = threading.Event()

    def _blocking_factory(**_kwargs):
        counter["n"] += 1  # 仅在持锁的 _create_engine 内执行，无计数竞态
        factory_entered.set()
        release_factory.wait(5)

        def _engine(_image_path: str):
            return []

        return _engine

    service = PaddleOCRService(models_root=models_root, engine_factory=_blocking_factory)

    preload_thread = threading.Thread(target=service.preload)
    preload_thread.start()
    assert factory_entered.wait(5)  # preload 已进入 _create_engine 并持有 _recognize_lock

    recognize_done = threading.Event()

    def _recognize() -> None:
        service.recognize(str(image_path))
        recognize_done.set()

    recognize_thread = threading.Thread(target=_recognize)
    recognize_thread.start()
    # preload 仍持锁，recognize 必须阻塞在 _recognize_lock 上
    assert not recognize_done.wait(0.3)

    release_factory.set()
    preload_thread.join(5)
    recognize_thread.join(5)

    assert recognize_done.is_set()
    assert counter["n"] == 1  # 并发下引擎只构建一次
```

并把现有 2 个依赖「构造即加载」的用例迁移。将 `tests/test_ocr_paddle_service.py:50-81` 这两个函数：

```python
def test_init_raises_e_ocr_002_when_engine_factory_fails(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")

    def _broken_factory(**_: object) -> object:
        raise RuntimeError("load failed")

    with pytest.raises(OCRServiceError) as exc:
        PaddleOCRService(models_root=models_root, engine_factory=_broken_factory)

    assert exc.value.code == "E_OCR_002"


def test_init_uses_local_model_paths_and_disables_download(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    captured: dict[str, object] = {}

    def _factory(**kwargs: object):
        captured.update(kwargs)

        def _engine(_image_path: str):
            return []

        return _engine

    PaddleOCRService(models_root=models_root, engine_factory=_factory)

    assert Path(str(captured["det_model_dir"])) == models_root / MODEL_DIRS["det"]
```

替换为（引擎加载延后到 `preload()`，故触发点改为 `preload()`）：

```python
def test_preload_raises_e_ocr_002_when_engine_factory_fails(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")

    def _broken_factory(**_: object) -> object:
        raise RuntimeError("load failed")

    service = PaddleOCRService(models_root=models_root, engine_factory=_broken_factory)

    with pytest.raises(OCRServiceError) as exc:
        service.preload()

    assert exc.value.code == "E_OCR_002"


def test_preload_uses_local_model_paths_and_disables_download(tmp_path: Path) -> None:
    models_root = _create_model_tree(tmp_path / "models")
    captured: dict[str, object] = {}

    def _factory(**kwargs: object):
        captured.update(kwargs)

        def _engine(_image_path: str):
            return []

        return _engine

    service = PaddleOCRService(models_root=models_root, engine_factory=_factory)
    service.preload()

    assert Path(str(captured["det_model_dir"])) == models_root / MODEL_DIRS["det"]
    assert Path(str(captured["rec_model_dir"])) == models_root / MODEL_DIRS["rec"]
    assert Path(str(captured["ori_model_dir"])) == models_root / MODEL_DIRS["ori"]
    assert Path(str(captured["doc_ori_model_dir"])) == models_root / MODEL_DIRS["doc_ori"]
    assert Path(str(captured["unwarp_model_dir"])) == models_root / MODEL_DIRS["unwarp"]
    assert captured["download_enabled"] is False
```

- [ ] **Step 2: 运行新测试，确认按预期失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ocr_paddle_service.py::test_construction_does_not_build_engine tests/test_ocr_paddle_service.py::test_preload_builds_engine_once_and_is_idempotent -v`
Expected: FAIL —— `AttributeError: 'PaddleOCRService' object has no attribute 'preload'`；`test_construction_does_not_build_engine` 因当前构造期即调用 factory 而 `counter["n"] == 1` 断言失败。

- [ ] **Step 3: 改 `__init__` 为懒加载**

将 `src/ocr/paddle_service.py:88-94`：

```python
        self._models_root = Path(models_root)
        self._model_paths = self._validate_model_paths(self._models_root)
        self._engine_factory = engine_factory or self._default_engine_factory
        self._recognize_lock = threading.Lock()
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine = self._create_engine(self._runtime_options)
        self._retry_engine_cache: dict[OCRRuntimeOptions, Callable[[str], Any]] = {}
```

替换为：

```python
        self._models_root = Path(models_root)
        self._model_paths = self._validate_model_paths(self._models_root)
        self._engine_factory = engine_factory or self._default_engine_factory
        self._recognize_lock = threading.Lock()
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine: Callable[[str], Any] | None = None
        self._retry_engine_cache: dict[OCRRuntimeOptions, Callable[[str], Any]] = {}
```

- [ ] **Step 4: 新增 `preload()` 与 `_ensure_engine_locked()`**

在 `update_runtime_options`（当前 `src/ocr/paddle_service.py:96`）之前插入：

```python
    def preload(self) -> None:
        """提前构建 OCR 引擎，使首次识别免于现场加载。幂等；供后台线程调用。"""
        with self._recognize_lock:
            self._ensure_engine_locked()

    def _ensure_engine_locked(self) -> Callable[[str], Any]:
        """懒建引擎。调用方必须已持有 ``self._recognize_lock``。"""
        if self._engine is None:
            self._engine = self._create_engine(self._runtime_options)
        return self._engine
```

- [ ] **Step 5: `_reset_runtime_locked` 改置空（惰性重建）**

将 `src/ocr/paddle_service.py:104-107`：

```python
    def _reset_runtime_locked(self, runtime_options: OCRRuntimeOptions | Mapping[str, Any]) -> None:
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine = self._create_engine(self._runtime_options)
        self._retry_engine_cache.clear()
```

替换为：

```python
    def _reset_runtime_locked(self, runtime_options: OCRRuntimeOptions | Mapping[str, Any]) -> None:
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine = None
        self._retry_engine_cache.clear()
```

- [ ] **Step 6: `_recognize_locked` 起始处兜底懒建**

在 `src/ocr/paddle_service.py:178`（`raise OCRServiceError("E_OCR_003", ...)` 那行之后、`offset = (0, 0)` 之前）插入一行，使无效图片仍先于建引擎被拒、有效图片在使用引擎前确保已建：

```python
            raise OCRServiceError("E_OCR_003", "Invalid image path or unsupported image type")

        self._ensure_engine_locked()

        offset = (0, 0)
```

> `_recognize_locked` 由 `recognize`（`src/ocr/paddle_service.py:166-167`）在持锁状态下调用，满足 `_ensure_engine_locked` 的持锁约定。后续 `self._engine`（line 194）与 `_get_retry_engine`（line 346 `return self._engine`）此时均非 None。

- [ ] **Step 7: 运行 paddle_service 全量测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ocr_paddle_service.py -v`
Expected: PASS（含新增 5 个用例与迁移后的 2 个用例；其余走 `recognize` 的旧用例不受影响）。

- [ ] **Step 8: Commit**

```bash
git add src/ocr/paddle_service.py tests/test_ocr_paddle_service.py
git commit -m "feat(ocr): PaddleOCRService 改懒加载并新增 preload，引擎不再构造期同步加载

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `RoutingOCRService.maybe_preload_local`（条件预热）

**Files:**
- Modify: `src/ocr/routing_service.py:45`（在 `recognize` 之前插入方法）
- Modify: `tests/test_ocr_routing_service.py:16-41`（`_RecordingService` 加 `preload`）
- Test: `tests/test_ocr_routing_service.py`（新增 2 用例）

- [ ] **Step 1: 写失败测试 —— 仅本地时预热**

在 `tests/test_ocr_routing_service.py` 的 `_RecordingService`（line 16-41）中新增 `preload` 计数。把 `recognize_calls: list[...]` 等字段区追加一行，并加方法：

在字段定义区（`update_calls: list[Any] = field(default_factory=list)` 之后）追加：

```python
    preload_calls: int = 0
```

在 `update_config` 方法之后追加：

```python
    def preload(self) -> None:
        self.preload_calls += 1
```

在文件末尾追加测试：

```python
def test_maybe_preload_local_preloads_when_offline() -> None:
    local, online = _make_services()
    config = _base_config(ocr_use_online=False)
    service = RoutingOCRService(local=local, online=online, config=config)

    service.maybe_preload_local()

    assert local.preload_calls == 1
    assert online.preload_calls == 0


def test_maybe_preload_local_skips_when_online() -> None:
    local, online = _make_services()
    config = _base_config(ocr_use_online=True)
    service = RoutingOCRService(local=local, online=online, config=config)

    service.maybe_preload_local()

    assert local.preload_calls == 0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ocr_routing_service.py::test_maybe_preload_local_preloads_when_offline -v`
Expected: FAIL —— `AttributeError: 'RoutingOCRService' object has no attribute 'maybe_preload_local'`。

- [ ] **Step 3: 实现 `maybe_preload_local`**

在 `src/ocr/routing_service.py` 的 `recognize` 方法（line 45）之前插入：

```python
    def maybe_preload_local(self) -> None:
        """仅当当前走本地 OCR 时，触发本地引擎预热；在线模式直接跳过。"""
        if not self._use_online:
            self._local.preload()
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ocr_routing_service.py -v`
Expected: PASS（含新增 2 用例；既有路由用例不受影响）。

- [ ] **Step 5: Commit**

```bash
git add src/ocr/routing_service.py tests/test_ocr_routing_service.py
git commit -m "feat(ocr): RoutingOCRService 新增 maybe_preload_local，仅本地 OCR 时预热

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 主窗口后台预热 worker + 独立指示器

**Files:**
- Modify: `src/ui/main_window.py`（新增 `_ModelPreloadWorker` 类；`_create_widgets` 区加指示器；`status_bar_layout` 区挂指示器；新增 `start_model_preload` + 槽 + 关闭清理；`__init__` 线程引用初始化；`closeEvent` 调清理）
- Test: `tests/test_ui_model_preload.py`（新建）

- [ ] **Step 1: 写失败测试 —— worker 同步行为**

新建 `tests/test_ui_model_preload.py`：

```python
from __future__ import annotations

import os

from PySide6.QtWidgets import QApplication

from src.ui.main_window import _ModelPreloadWorker

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app() -> QApplication:
    app = QApplication.instance()
    return app or QApplication([])


class _StubOcrService:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises

    def maybe_preload_local(self) -> None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")


def test_worker_calls_maybe_preload_local_and_emits_finished() -> None:
    _app()
    service = _StubOcrService()
    worker = _ModelPreloadWorker(ocr_service=service)
    events: list[object] = []
    worker.finished.connect(lambda: events.append("finished"))
    worker.failed.connect(lambda msg: events.append(("failed", msg)))

    worker.run()

    assert service.calls == 1
    assert events == ["finished"]


def test_worker_emits_failed_on_exception() -> None:
    _app()
    service = _StubOcrService(raises=True)
    worker = _ModelPreloadWorker(ocr_service=service)
    events: list[object] = []
    worker.finished.connect(lambda: events.append("finished"))
    worker.failed.connect(lambda msg: events.append(("failed", msg)))

    worker.run()

    assert service.calls == 1
    assert events == [("failed", "boom")]
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ui_model_preload.py -v`
Expected: FAIL —— `ImportError: cannot import name '_ModelPreloadWorker' from 'src.ui.main_window'`。

- [ ] **Step 3: 新增 `_ModelPreloadWorker` 类**

在 `src/ui/main_window.py` 的 `_RecognitionWorker` 类定义（结束于 line 478 `self.engine_event.emit(event)`）之后、`class _TitleBarControlButton`（line 481）之前插入：

```python
class _ModelPreloadWorker(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self, *, ocr_service: Any) -> None:
        super().__init__()
        self._ocr_service = ocr_service

    def run(self) -> None:
        try:
            maybe_preload = getattr(self._ocr_service, "maybe_preload_local", None)
            if callable(maybe_preload):
                maybe_preload()
            self.finished.emit()
        except Exception as exc:  # 后台线程绝不可崩进程
            self.failed.emit(str(exc))
```

- [ ] **Step 4: 运行 worker 测试，确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ui_model_preload.py -v`
Expected: PASS（2 用例）。

- [ ] **Step 5: `__init__` 初始化预热线程引用**

在 `src/ui/main_window.py:699-700`：

```python
        self._recognition_thread: QThread | None = None
        self._recognition_worker: _RecognitionWorker | None = None
```

之后追加：

```python
        self._model_preload_thread: QThread | None = None
        self._model_preload_worker: _ModelPreloadWorker | None = None
```

- [ ] **Step 6: 创建独立指示器 widget（默认隐藏）**

在 `src/ui/main_window.py:887-891` 状态栏 widget 创建区：

```python
        self.status_dot = _StatusDot()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusBarLabel")
        self.status_detail_label = QLabel("")
        self.status_detail_label.setObjectName("statusDetailLabel")
```

之后追加：

```python
        self.model_preload_dot = _StatusDot()
        self.model_preload_label = QLabel("本地模型加载中…")
        self.model_preload_label.setObjectName("statusDetailLabel")
        self.model_preload_indicator = QWidget()
        _model_preload_layout = QHBoxLayout(self.model_preload_indicator)
        _model_preload_layout.setContentsMargins(0, 0, 0, 0)
        _model_preload_layout.setSpacing(6)
        _model_preload_layout.addWidget(self.model_preload_dot)
        _model_preload_layout.addWidget(
            self.model_preload_label, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self.model_preload_indicator.setVisible(False)
```

- [ ] **Step 7: 把指示器挂到状态栏右侧**

在 `src/ui/main_window.py:1094`：

```python
        status_bar_layout.addStretch(1)
```

之后、`status_bar_layout.addWidget(self.clear_console_button)`（line 1095）之前插入：

```python
        status_bar_layout.addWidget(self.model_preload_indicator)
```

- [ ] **Step 8: 新增 `start_model_preload` + 完成/失败/线程结束槽**

在 `src/ui/main_window.py` 的 `_start_recognition_in_background`（line 1626-1642）之后、`_on_recognition_thread_finished`（line 1644）之前插入：

```python
    def start_model_preload(self) -> None:
        """窗口显示后触发本地模型后台预热（仅 ocr_service 提供该能力时）。"""
        engine = getattr(self.controller, "engine", None)
        ocr_service = getattr(engine, "ocr_service", None)
        if ocr_service is None or not hasattr(ocr_service, "maybe_preload_local"):
            return
        self.model_preload_dot.set_state("running")
        self.model_preload_indicator.setVisible(True)

        thread = QThread(self)
        worker = _ModelPreloadWorker(ocr_service=ocr_service)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_model_preload_finished)
        worker.failed.connect(self._on_model_preload_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._on_model_preload_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._model_preload_thread = thread
        self._model_preload_worker = worker
        thread.start()

    def _on_model_preload_finished(self) -> None:
        self.model_preload_indicator.setVisible(False)

    def _on_model_preload_failed(self, message: str) -> None:
        self.model_preload_dot.set_state("error")
        self.model_preload_label.setText("本地模型加载失败")
        self.append_log(level="warn", message=f"Local OCR model preload failed: {message}")

    def _on_model_preload_thread_finished(self) -> None:
        self._model_preload_worker = None
        self._model_preload_thread = None
```

- [ ] **Step 9: 关闭时清理预热线程**

在 `src/ui/main_window.py` 的 `closeEvent`（line 1648-1652）中，`self._shutdown_recognition()` 之后追加一行：

```python
    def closeEvent(self, event: Any) -> None:
        # 关闭时若后台识别线程仍在跑，必须先请求停止并等它结束，否则运行中的 QThread
        # 被销毁会触发 Qt abort（SIGABRT）。在线 OCR 的长轮询会拉长这个窗口。
        self._shutdown_recognition()
        self._shutdown_model_preload()
        super().closeEvent(event)
```

并在 `_shutdown_recognition`（结束于 line 1669 `thread.wait(15000)`）之后插入：

```python
    def _shutdown_model_preload(self) -> None:
        thread = self._model_preload_thread
        if thread is None or not thread.isRunning():
            return
        # 模型加载是不可中断的同步调用，无法提前 cancel；quit 仅停事件循环，
        # wait 会阻塞到 worker.run() 返回（最坏约一次模型加载耗时）。
        thread.quit()
        thread.wait(15000)
```

- [ ] **Step 10: 运行 UI worker 测试 + paddle/routing 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ui_model_preload.py tests/test_ocr_paddle_service.py tests/test_ocr_routing_service.py -v`
Expected: PASS。

- [ ] **Step 11: Commit**

```bash
git add src/ui/main_window.py tests/test_ui_model_preload.py
git commit -m "feat(ui): 主窗口后台预热本地模型 + 状态栏独立加载指示器

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `app.py` 触发预热 + 启动 smoke 验证

**Files:**
- Modify: `src/app.py:132-139`（`main`）

- [ ] **Step 1: 在 `window.show()` 后触发预热**

将 `src/app.py:132-139`：

```python
def main() -> int:
    _suppress_subprocess_console()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_app_icon())
    _register_fonts()
    window = build_main_window()
    window.show()
    return app.exec()
```

替换为：

```python
def main() -> int:
    _suppress_subprocess_console()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_app_icon())
    _register_fonts()
    window = build_main_window()
    window.show()
    window.start_model_preload()
    return app.exec()
```

- [ ] **Step 2: 全量测试回归**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: PASS（全部用例；尤其 `tests/test_ocr_paddle_service.py`、`tests/test_ocr_routing_service.py`、`tests/test_ui_model_preload.py`、`tests/test_ui_qt_main_window.py`）。

- [ ] **Step 3: 源码态 smoke 验证窗口秒开**

Run: `.venv/Scripts/python.exe -c "import time,sys; sys.argv=['app']; from PySide6.QtWidgets import QApplication; t=time.perf_counter(); from src.app import build_main_window; app=QApplication.instance() or QApplication(sys.argv); w=build_main_window(); print(f'build_main_window: {(time.perf_counter()-t)*1000:.0f} ms'); w.start_model_preload(); print('preload kicked off (background)')"`
Expected: `build_main_window` 从原 ~8000ms 降到数百 ms（不再打印 `Creating model: ...`，因构造期不再加载）；预热在后台进行。

- [ ] **Step 4: 人工运行 app 验证指示器（windowed 行为）**

Run: `.venv/Scripts/python.exe src/app.py`
Expected：窗口立即出现（秒级），状态栏右侧短暂显示「● 本地模型加载中…」脉冲提示，约数秒后消失；随后点击一张图片识别能正常返回（引擎已预热或懒建兜底）。在设置里将 OCR 切到「在线」并重启，应不出现该提示。

- [ ] **Step 5: Commit**

```bash
git add src/app.py
git commit -m "feat(app): 窗口显示后触发本地模型后台预热，启动不再被模型加载阻塞

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**

| spec 要求 | 对应 Task |
|-----------|-----------|
| `__init__` 不再加载（懒加载） | Task 1 Step 3 |
| `preload()` / `_ensure_engine_locked()` | Task 1 Step 4 |
| `_recognize_locked` 兜底懒建 | Task 1 Step 6 |
| `_reset_runtime_locked` 改置空 | Task 1 Step 5 |
| `maybe_preload_local`（仅本地） | Task 2 Step 3 |
| `app.py` 先显示后预热 | Task 4 Step 1 |
| `_ModelPreloadWorker` + finished/failed | Task 3 Step 3 |
| 独立指示器（不复用 status_dot/label） | Task 3 Step 6-7 |
| `controller.engine.ocr_service` + getattr 降级 | Task 3 Step 8 |
| 关闭清理 | Task 3 Step 9 |
| 测试：构造不加载/幂等/reset 重建/条件预热 | Task 1 Step 1、Task 2 Step 1 |
| 测试：并发持锁阻塞 + 只加载一次（spec line 94） | Task 1 Step 1 `test_concurrent_preload_and_recognize_build_engine_once` |

无遗漏。运行时「在线→本地」切换不重新预热为 spec 明示的非目标，无需 Task。

**2. Placeholder scan:** 无 TBD/TODO；每个改码步骤均给出完整 old→new 代码与确切命令。

**3. Type consistency:** 全程方法名一致——`preload()`、`_ensure_engine_locked()`、`maybe_preload_local()`、`_ModelPreloadWorker`、`start_model_preload()`、`_on_model_preload_finished/_failed/_thread_finished`、`_shutdown_model_preload()`、指示器 `model_preload_dot/label/indicator`。Worker 信号 `finished()` / `failed(str)` 与 `_RecognitionWorker` 一致。`_StatusDot.set_state` 用值 `"running"`/`"error"` 均在其 `_COLORS` 支持集内（`ready/running/error/idle`），其中 `"running"` 触发脉冲动画。
