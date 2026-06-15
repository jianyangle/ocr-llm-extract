from __future__ import annotations

import logging
import sys
from pathlib import Path

# Support direct script execution: `python src/app.py`
if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from PySide6.QtCore import QSize
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from src.extract.llm_extractor import LLMExtractor
from src.extract.provider_ollama import OllamaAdapter
from src.extract.provider_openai import OpenAICompatibleAdapter
from src.io.config_store import ConfigStore
from src.io.excel_writer import ExcelWriter
from src.io.log_store import LogStore
from src.core.online_pdf_processor import OnlinePdfOCRProcessor
from src.ocr.online_service import OnlineOCRService
from src.ocr.paddle_service import PaddleOCRService
from src.ocr.routing_service import RoutingOCRService
from src.ui.main_window import MainWindow, MainWorkbenchController
from src.ui.settings_dialog import SettingsController

logger = logging.getLogger(__name__)


def _suppress_subprocess_console() -> None:
    """消除 windowed 冻结包启动时的控制台黑屏闪现。

    paddle 在导入期会执行 `where ccache` 探测编译缓存（`paddle.utils.cpp_extension`），
    无控制台的 GUI 进程拉起该控制台子进程时会瞬时弹出一个 cmd 窗口。为所有子进程默认
    叠加 `CREATE_NO_WINDOW`，既不弹窗也不影响其 stdout 捕获。仅在 Windows 冻结包生效。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import subprocess

    _orig_init = subprocess.Popen.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_init  # type: ignore[method-assign]


def _register_fonts() -> None:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        fonts_dir = Path(meipass) / "data" / "fonts"
    else:
        fonts_dir = Path(__file__).resolve().parents[1] / "data" / "fonts"
    if not fonts_dir.is_dir():
        return
    for font_file in fonts_dir.glob("*.[to]tf"):
        font_id = QFontDatabase.addApplicationFont(str(font_file))
        if font_id < 0:
            logger.warning("Failed to load font: %s", font_file.name)


def _app_icon() -> QIcon:
    """构建多尺寸应用图标。

    任务栏会按需选取最接近的尺寸，提供逐尺寸精渲染的 PNG 可避免
    窗口管理器对单张大图降采样导致的发灰、糊化。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        icon_root = Path(meipass) / "src" / "ui" / "assets" / "icons" / "app_icon"
    else:
        icon_root = Path(__file__).resolve().parent / "ui" / "assets" / "icons" / "app_icon"

    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        png = icon_root / f"OLE_{size}.png"
        if png.is_file():
            icon.addFile(str(png), QSize(size, size))
    return icon


def _resolve_models_root() -> Path:
    # Bundled models are added via PyInstaller --add-data (under _MEIPASS); in dev
    # they live next to the source tree at the project root.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "models"
    return Path(__file__).resolve().parents[1] / "models"


def build_main_window(project_root: str | Path | None = None) -> MainWindow:
    root = Path(project_root) if project_root else Path.cwd()
    models_root = Path(project_root) / "models" if project_root else _resolve_models_root()
    config_store = ConfigStore()
    config = config_store.load()
    log_store = LogStore()

    ocr_service = RoutingOCRService(
        local=PaddleOCRService(
            models_root=models_root,
            runtime_options=PaddleOCRService.runtime_options_from_app_config(config),
        ),
        online=OnlineOCRService(
            online_config=RoutingOCRService.runtime_options_from_app_config(config).online_config,
        ),
        config=config,
    )
    online_pdf_processor = OnlinePdfOCRProcessor()
    extractor = LLMExtractor(
        openai_adapter=OpenAICompatibleAdapter(event_logger=log_store.log_record),
        ollama_adapter=OllamaAdapter(),
    )
    excel_writer = ExcelWriter(project_root=root)

    main_controller = MainWorkbenchController(
        config=config,
        ocr_service=ocr_service,
        extractor=extractor,
        excel_writer=excel_writer,
        log_store=log_store,
        online_pdf_processor=online_pdf_processor,
    )
    settings_controller = SettingsController(config_store=config_store)
    return MainWindow(controller=main_controller, settings_controller=settings_controller)


def main() -> int:
    _suppress_subprocess_console()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_app_icon())
    _register_fonts()
    window = build_main_window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # 冻结包（windowed）无控制台，启动期异常只会弹出难懂的对话框；
        # 把完整 traceback 落盘到 exe 同级目录，便于排查。
        if getattr(sys, "frozen", False):
            import traceback

            crash_log = Path(sys.executable).parent / "startup_crash.log"
            try:
                crash_log.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
        raise
