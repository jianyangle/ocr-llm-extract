from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QRect, QThread, QSize, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.engine_events import (
    EngineEvent,
    TaskAutoDispatchTriggered,
    TaskFailed,
    TaskOcrCompleted,
    TaskPageResultStreamed,
    TaskOcrProgressed,
    TaskProgressed,
    TaskStarted,
    TaskSucceeded,
)
from src.core.task_engine import TaskEngine
from src.domain.schemas import AppConfig, ExtractRow, WriteSummary
from src.io.excel_writer import DEFAULT_OUTPUT_FILENAME, ExcelWriter
from src.io.log_store import LogStore
from src.extract.template_catalog import TemplateCatalog, project_active_template_config
from src.ui.task_review_coordinator import TaskReviewCoordinator
from src.ui.task_review_event_translator import TaskReviewEventTranslator
from src.ui.settings_dialog import SettingsController, SettingsDialog
from src.ui.styled_button import _StyledButton
from src.ui.table_delegates import PROGRESS_ROLE, ProgressDelegate, ResultTableDelegate, TaskQueueDelegate
from src.ui.theme import COLORS, RADIUS, STATUS_BADGE_STYLES, TYPOGRAPHY, generate_main_qss, lerp_color
from src.ui.task_review_projection import InspectorRenderData, TaskReviewExportRow, TaskReviewProjection


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}
INSPECTOR_OCR_PLACEHOLDER = "暂无 OCR 文本"
INSPECTOR_EMPTY_TIPS = "选择一项任务或结果行以查看详情。"
TASK_SOURCE_DISPLAY_PREFIX_CHARS = 15
TASK_QUEUE_PROGRESS_COLUMN_WIDTH = 52
TASK_QUEUE_ACTIONS_COLUMN_WIDTH = 58
TASK_QUEUE_ACTION_BUTTON_SIZE = 24
INSPECTOR_PANEL_HEIGHT = 246
BOTTOM_STATUS_BAR_HEIGHT = 34


def _resolve_default_excel_root() -> Path:
    # Frozen builds: PyInstaller's _MEIPASS would land us in a temp dir that gets cleaned up,
    # so anchor the writable default Excel next to the executable instead.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resolve_resource_root() -> Path:
    # Read-only bundled assets (icons) are added via PyInstaller --add-data, which
    # lands them under _MEIPASS; in dev they sit at the project root.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


_PROJECT_ROOT = _resolve_default_excel_root()
_RESOURCE_ROOT = _resolve_resource_root()
_DEFAULT_EXCEL_FALLBACK = str(_PROJECT_ROOT / DEFAULT_OUTPUT_FILENAME)
_TASK_QUEUE_ICON_ROOT = _RESOURCE_ROOT / "data" / "icon_1rfurz1zeyz"
_TASK_QUEUE_PDF_COLLAPSED_ICON = _TASK_QUEUE_ICON_ROOT / "window-right.svg"
_TASK_QUEUE_PDF_EXPANDED_ICON = _TASK_QUEUE_ICON_ROOT / "window-down.svg"
_TASK_QUEUE_ACTION_PAUSE_ICON = _TASK_QUEUE_ICON_ROOT / "zanting.svg"
_TASK_QUEUE_ACTION_RESUME_ICON = _TASK_QUEUE_ICON_ROOT / "jixu.svg"
_TASK_QUEUE_ACTION_DELETE_ICON = _TASK_QUEUE_ICON_ROOT / "shanchu.svg"
_ICONS_DIR = _RESOURCE_ROOT / "data" / "icons"
_TOOLBAR_ICON_DIR = _RESOURCE_ROOT / "data" / "icons" / "toolbar"

logger = logging.getLogger(__name__)


class _StatusDot(QWidget):
    _COLORS = {
        "ready": COLORS["status_ready"],
        "running": COLORS["status_running"],
        "error": COLORS["status_error"],
        "idle": COLORS["status_idle"],
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(15, 15)
        self._state = "ready"
        self._glow_radius: float = 3.0
        self._current_color = QColor(self._COLORS["ready"])
        self._color_start = QColor(self._current_color)
        self._color_end = QColor(self._current_color)
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setStartValue(3.0)
        self._pulse_anim.setEndValue(5.0)
        self._pulse_anim.setDuration(1500)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.valueChanged.connect(self._on_pulse_tick)
        self._color_anim = QVariantAnimation(self)
        self._color_anim.setStartValue(0.0)
        self._color_anim.setEndValue(1.0)
        self._color_anim.setDuration(200)
        self._color_anim.valueChanged.connect(self._on_color_tick)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state

        target_color = QColor(self._COLORS.get(state, self._COLORS["idle"]))
        self._color_anim.stop()
        self._color_start = QColor(self._current_color)
        self._color_end = target_color
        self._color_anim.setStartValue(0.0)
        self._color_anim.setEndValue(1.0)
        self._color_anim.start()

        if state == "running":
            self._start_pulse()
        else:
            self._stop_pulse()

    def _start_pulse(self) -> None:
        if self._pulse_anim.state() == QVariantAnimation.State.Running:
            return
        self._pulse_anim.start()

    def _stop_pulse(self) -> None:
        self._pulse_anim.stop()
        self._glow_radius = 3.0
        self.update()

    def _on_pulse_tick(self, value: Any) -> None:
        self._glow_radius = float(value)
        self.update()

    def _on_color_tick(self, value: Any) -> None:
        self._current_color = lerp_color(self._color_start, self._color_end, float(value))
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self._current_color)
        glow = QColor(color)
        glow.setAlphaF(0.15)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._glow_radius
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))
        painter.setBrush(color)
        dot_r = 3.5
        painter.drawEllipse(int(cx - dot_r), int(cy - dot_r), int(2 * dot_r), int(2 * dot_r))
        painter.end()


class _EmptyStateIconContainer(QWidget):
    def __init__(
        self,
        *,
        svg_path: str,
        container_size: int,
        border_radius: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(container_size, container_size)
        self._renderer = QSvgRenderer(svg_path)
        self._border_radius = border_radius

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(COLORS["border_dashed"]), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(COLORS["canvas"]))
        painter.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, self._border_radius, self._border_radius)

        if self._renderer.isValid():
            svg_size = self._renderer.defaultSize()
            x = (self.width() - svg_size.width()) // 2
            y = (self.height() - svg_size.height()) // 2
            self._renderer.render(painter, QRect(x, y, svg_size.width(), svg_size.height()).toRectF())
        painter.end()


class _EmptyStateOverlay(QWidget):
    def __init__(
        self,
        *,
        title: str,
        description: str = "",
        icon_svg_path: str | None = None,
        icon_container_size: int = 44,
        icon_border_radius: int = 12,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        layout.addStretch(1)

        if icon_svg_path and Path(icon_svg_path).exists():
            icon_container = _EmptyStateIconContainer(
                svg_path=icon_svg_path,
                container_size=icon_container_size,
                border_radius=icon_border_radius,
            )
            layout.addWidget(icon_container, alignment=Qt.AlignmentFlag.AlignCenter)
        else:
            icon_label = QLabel("□")
            icon_label.setObjectName("emptyStateIcon")
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setObjectName("emptyStateTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_label = QLabel(description)
        description_label.setObjectName("emptyStateDescription")
        description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_label.setWordWrap(True)
        layout.addWidget(title_label)
        if description:
            layout.addWidget(description_label)
        layout.addStretch(1)

    def sync_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.setGeometry(parent.rect())


class _InspectorOcrTextEdit(QPlainTextEdit):
    """Read-only OCR text widget that keeps long lines inside its own scroll area."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setContentsMargins(0, 0, 0, 0)
        self.document().setDocumentMargin(2)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.document().documentLayout().documentSizeChanged.connect(self._sync_content_size)
        self.setText(text)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        if self.toPlainText() != value:
            self.setPlainText(value)
        self._sync_content_size()

    def _sync_content_size(self, *_: object) -> None:
        doc_size = self.document().size()
        margins = self.contentsMargins()
        frame_size = self.frameWidth() * 2
        doc_margin = int(self.document().documentMargin() * 2)
        height = int(doc_size.height()) + margins.top() + margins.bottom() + frame_size + doc_margin + 8
        self.setMinimumWidth(220)
        self.setMinimumHeight(max(36, height))
        self.updateGeometry()


class MainWorkbenchController:
    """UI-agnostic controller for main workbench behaviors."""

    def __init__(
        self,
        *,
        config: AppConfig,
        ocr_service: object,
        extractor: object,
        pdf_adapter: object | None = None,
        excel_writer: ExcelWriter,
        log_store: LogStore | None = None,
        online_pdf_processor: object | None = None,
    ) -> None:
        self.log_store = log_store
        self._event_listeners: list[Callable[[EngineEvent], None]] = []
        self.engine = TaskEngine(
            config=config,
            ocr_service=ocr_service,
            extractor=extractor,
            pdf_adapter=pdf_adapter,
            event_sink=self._handle_engine_event,
            online_pdf_processor=online_pdf_processor,
        )
        self.excel_writer = excel_writer

    @property
    def tasks(self):
        return self.engine.tasks

    @property
    def result_rows(self) -> list[ExtractRow]:
        return self.engine.result_rows

    def add_text_task(self, text: str) -> str:
        return self.engine.add_text(text)

    def add_image_task(self, image_path: str) -> str:
        return self.engine.add_image(image_path)

    def add_pdf_task(self, pdf_path: str) -> str:
        return self.engine.add_pdf(pdf_path)

    def start_recognition(self) -> None:
        self.engine.start()

    def request_stop(self) -> None:
        self.engine.request_stop()

    def write_excel(self, output_path: str) -> WriteSummary:
        configured_default = self.engine.config.default_excel_path.strip()
        effective_output = output_path.strip() if output_path else ""
        if not effective_output:
            effective_output = configured_default

        summary = self.excel_writer.write_result_rows(self.result_rows, effective_output)
        output_resolution = getattr(self.excel_writer, "last_output_resolution", None)
        if isinstance(output_resolution, dict):
            self._log_record(
                "EVT-EXPORT-001",
                {
                    "original_output_path": str(output_resolution.get("original_output_path", "")),
                    "resolved_output_path": str(output_resolution.get("resolved_output_path", summary.output_path)),
                    "original_filename": str(output_resolution.get("original_filename", "")),
                    "normalized_filename": str(output_resolution.get("normalized_filename", "")),
                    "resolved_filename": str(output_resolution.get("resolved_filename", "")),
                },
            )
        # Persist runtime effective path in-memory for FR-013.
        self.engine.config.default_excel_path = summary.output_path
        return summary

    def write_result_review_rows(self, rows: list[TaskReviewExportRow], output_path: str) -> WriteSummary:
        configured_default = self.engine.config.default_excel_path.strip()
        effective_output = output_path.strip() if output_path else ""
        if not effective_output:
            effective_output = configured_default

        summary = self.excel_writer.write_review_rows(rows, effective_output)
        output_resolution = getattr(self.excel_writer, "last_output_resolution", None)
        if isinstance(output_resolution, dict):
            self._log_record(
                "EVT-EXPORT-001",
                {
                    "original_output_path": str(output_resolution.get("original_output_path", "")),
                    "resolved_output_path": str(output_resolution.get("resolved_output_path", summary.output_path)),
                    "original_filename": str(output_resolution.get("original_filename", "")),
                    "normalized_filename": str(output_resolution.get("normalized_filename", "")),
                    "resolved_filename": str(output_resolution.get("resolved_filename", "")),
                },
            )
        self.engine.config.default_excel_path = summary.output_path
        return summary

    def delete_task(self, task_id: str) -> None:
        self.engine.delete_task(task_id)

    def retry_task(self, task_id: str) -> None:
        self.engine.retry_task(task_id)

    def pause_task(self, task_id: str) -> None:
        self.engine.pause_task(task_id)

    def resume_task(self, task_id: str) -> None:
        self.engine.resume_task(task_id)

    def pause_all_tasks(self) -> None:
        self.engine.pause_all()

    def resume_all_tasks(self) -> None:
        self.engine.resume_all()

    def clear_all_tasks(self) -> None:
        self.engine.clear_all()

    def _log_event(self, event: EngineEvent) -> None:
        if self.log_store is None:
            return
        self.log_store.log_event(event)

    def _log_record(self, event_type: str, payload: dict[str, object]) -> None:
        if self.log_store is None:
            return
        self.log_store.log_record(event_type, payload)

    def _handle_engine_event(self, event: EngineEvent) -> None:
        self._log_event(event)
        if not self._event_listeners:
            return
        for listener in list(self._event_listeners):
            try:
                listener(event)
            except Exception:
                # Event listeners should not break engine flow.
                continue

    def subscribe_engine_events(self, listener: Callable[[EngineEvent], None]) -> None:
        if listener in self._event_listeners:
            return
        self._event_listeners.append(listener)

    def unsubscribe_engine_events(self, listener: Callable[[EngineEvent], None]) -> None:
        self._event_listeners = [existing for existing in self._event_listeners if existing != listener]

    def log_event(self, event_type: str, payload: dict[str, object]) -> None:
        self._log_record(event_type, payload)


class _RecognitionWorker(QObject):
    finished = Signal()
    failed = Signal(str)
    engine_event = Signal(object)

    def __init__(self, *, controller: MainWorkbenchController | Any) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        subscribed = False
        if hasattr(self._controller, "subscribe_engine_events"):
            self._controller.subscribe_engine_events(self._emit_engine_event)
            subscribed = True
        try:
            self._controller.start_recognition()
            self.finished.emit()
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))
        finally:
            if subscribed and hasattr(self._controller, "unsubscribe_engine_events"):
                self._controller.unsubscribe_engine_events(self._emit_engine_event)

    def _emit_engine_event(self, event: EngineEvent) -> None:
        self.engine_event.emit(event)


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


class _TitleBarControlButton(QPushButton):
    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        icon_color = QColor("#ffffff") if self._kind == "close" and self.underMouse() else QColor(COLORS["fg_muted"])
        painter.setPen(QPen(icon_color, 1.2))
        cx = self.width() // 2
        cy = self.height() // 2

        if self._kind == "minimize":
            painter.drawLine(cx - 5, cy, cx + 5, cy)
        elif self._kind == "maximize":
            if self.window().isMaximized():
                painter.drawRect(cx - 3, cy - 5, 8, 8)
                painter.drawRect(cx - 5, cy - 3, 8, 8)
            else:
                painter.drawRect(cx - 5, cy - 5, 10, 10)
        elif self._kind == "close":
            painter.drawLine(cx - 5, cy - 5, cx + 5, cy + 5)
            painter.drawLine(cx + 5, cy - 5, cx - 5, cy + 5)

        painter.end()


class CustomTitleBar(QWidget):
    settings_clicked = Signal()

    _TITLEBAR_HEIGHT = 36
    _ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._TITLEBAR_HEIGHT)
        self.setObjectName("customTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._drag_pos: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(0)

        app_icon_label = QLabel()
        # 矢量 SVG 按屏幕 devicePixelRatio 渲染，避免位图非整数缩放导致的模糊
        app_icon = QIcon(str(self._ICON_DIR / "app_icon" / "OLE.svg"))
        screen = self.screen() or QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        app_icon_label.setPixmap(app_icon.pixmap(QSize(20, 20), dpr))
        app_icon_label.setFixedSize(20, 20)
        layout.addWidget(app_icon_label)
        layout.addSpacing(8)

        title_label = QLabel("OCR-LLM-Extract")
        title_label.setObjectName("titleBarLabel")
        layout.addWidget(title_label)
        layout.addStretch(1)

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("titleBarSettingsButton")
        self.settings_button.setToolTip("设置")
        self.settings_button.setAccessibleName("设置")
        self.settings_button.setFixedSize(36, 36)
        settings_pixmap = QPixmap(str(self._ICON_DIR / "NMStubiao-.svg"))
        if not settings_pixmap.isNull():
            painter = QPainter(settings_pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(settings_pixmap.rect(), QColor(COLORS["fg_muted"]))
            painter.end()
        self.settings_button.setIcon(QIcon(settings_pixmap))
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(self.settings_button)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedSize(1, 18)
        separator.setObjectName("titleBarSeparator")
        layout.addSpacing(2)
        layout.addWidget(separator, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(2)

        self.min_button = _TitleBarControlButton("minimize")
        self.min_button.setObjectName("titleBarMinButton")
        self.min_button.setToolTip("最小化")
        self.min_button.setFixedSize(46, 36)
        layout.addWidget(self.min_button)

        self.max_button = _TitleBarControlButton("maximize")
        self.max_button.setObjectName("titleBarMaxButton")
        self.max_button.setToolTip("最大化")
        self.max_button.setFixedSize(46, 36)
        layout.addWidget(self.max_button)

        self.close_button = _TitleBarControlButton("close")
        self.close_button.setObjectName("titleBarCloseButton")
        self.close_button.setToolTip("关闭")
        self.close_button.setFixedSize(46, 36)
        layout.addWidget(self.close_button)

        self.min_button.clicked.connect(self._on_minimize)
        self.max_button.clicked.connect(self._on_maximize)
        self.close_button.clicked.connect(self._on_close)

    def _on_minimize(self) -> None:
        self.window().showMinimized()

    def _on_maximize(self) -> None:
        if self.window().isMaximized():
            self.window().showNormal()
        else:
            self.window().showMaximized()
        self._update_max_button_tooltip()

    def _on_close(self) -> None:
        self.window().close()

    def _update_max_button_tooltip(self) -> None:
        self.max_button.setToolTip("还原" if self.window().isMaximized() else "最大化")

    def _is_interactive_child_at(self, pos: QPoint) -> bool:
        child = self.childAt(pos)
        while child is not None:
            if isinstance(child, QPushButton):
                return True
            child = child.parentWidget()
        return False

    def _start_system_move(self) -> bool:
        window_handle = self.window().windowHandle()
        if window_handle is None:
            return False
        return bool(window_handle.startSystemMove())

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._is_interactive_child_at(event.position().toPoint()):
            if self._start_system_move():
                self._drag_pos = None
            else:
                self._drag_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_pos is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return

        win = self.window()
        if win.isMaximized():
            ratio = event.position().x() / max(1, self.width())
            ratio = min(1.0, max(0.0, ratio))
            win.showNormal()
            new_x = int(event.globalPosition().x() - win.width() * ratio)
            new_y = int(event.globalPosition().y() - event.position().y())
            win.move(new_x, max(0, new_y))
            self._drag_pos = event.globalPosition().toPoint()
            self._update_max_button_tooltip()
        else:
            delta = event.globalPosition().toPoint() - self._drag_pos
            win.move(win.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event: Any) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._is_interactive_child_at(event.position().toPoint()):
            self._on_maximize()
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    """Spec-aligned workbench shell with top/left/center/right/bottom regions."""

    _RESIZE_MARGIN = 5
    _INSPECTOR_BADGE_MAP = {
        "pending": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "running": ("处理中", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
        "running_ocr": ("处理中", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
        "OCR中": ("处理中", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
        "running_extract": ("处理中", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
        "抽取中": ("处理中", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
        "done": ("已完成", "#EBF8F1", "#D2EFDD", "#138952"),
        "Done": ("已完成", "#EBF8F1", "#D2EFDD", "#138952"),
        "已完成": ("已完成", "#EBF8F1", "#D2EFDD", "#138952"),
        "success": ("已完成", "#EBF8F1", "#D2EFDD", "#138952"),
        "completed": ("已完成", "#EBF8F1", "#D2EFDD", "#138952"),
        "failed": ("失败", "#FCEEEE", "#F4D6D5", "#C9342F"),
        "Failed": ("失败", "#FCEEEE", "#F4D6D5", "#C9342F"),
        "失败": ("失败", "#FCEEEE", "#F4D6D5", "#C9342F"),
        "paused": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "Paused": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "已暂停": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "Pending": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "等待中": ("等待中", "#FFFFFF", "#E0E0E5", "#73737A"),
        "empty": ("跳过", "#F4F4F6", "#E5E5EA", "#52525B"),
        "跳过": ("跳过", "#F4F4F6", "#E5E5EA", "#52525B"),
    }

    def __init__(
        self,
        *,
        controller: MainWorkbenchController | Any,
        settings_controller: SettingsController | Any | None = None,
        confirm_write_fn: Callable[[dict[str, int | str]], bool] | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.settings_controller = settings_controller
        self.confirm_write_fn = confirm_write_fn or self._default_confirm_write
        self._console_auto_expanded_once = False
        self._current_schema: list[str] = []
        self._ui_recognition_running = False
        self._recognition_thread: QThread | None = None
        self._recognition_worker: _RecognitionWorker | None = None
        self._model_preload_thread: QThread | None = None
        self._model_preload_worker: _ModelPreloadWorker | None = None
        self._task_review_projection = TaskReviewProjection(
            ocr_placeholder=INSPECTOR_OCR_PLACEHOLDER,
            empty_tips=INSPECTOR_EMPTY_TIPS,
        )
        self._task_review_coordinator = TaskReviewCoordinator(
            projection=self._task_review_projection,
            event_translator=TaskReviewEventTranslator(),
        )
        self._task_table_nodes: list[dict[str, object]] = []
        self._rendered_result_rows: list[dict[str, object]] = []
        self._inspector_selected_task_id: str | None = None
        self._inspector_selected_page_index: int | None = None
        self._inspector_syncing_selection = False
        self._last_inspector_data: InspectorRenderData | None = None
        self._resize_edges: list[str] = []
        self._resize_start_pos: QPoint | None = None
        self._resize_start_geo: QRect | None = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(800, 600)
        self.setWindowTitle("OCR 抽取工作台")
        self.resize(1366, 840)

        self._init_widgets()
        self._build_layout()
        self._bind_events()
        self._apply_style()
        self._refresh_all_tables()
        self._refresh_active_template_combo()
        self._refresh_controls()
        self._set_console_visible(False)

    def _init_widgets(self) -> None:
        # Top global actions
        self.top_control_bar = QWidget()
        self.top_control_bar.setObjectName("topControlBar")
        self.add_text_button = _StyledButton("添加文本", variant="default")
        self.add_file_button = _StyledButton("添加文件", variant="default")
        self.start_button = _StyledButton("全部开始", variant="primary")
        self.pause_all_button = _StyledButton("全部暂停", variant="default")
        self.clear_all_button = _StyledButton("全部清空", variant="danger")
        self.write_button = _StyledButton("写入 Excel", variant="success")
        self.excel_path_button = _StyledButton("Excel 路径", variant="default")
        self.title_bar = CustomTitleBar(self)
        self.open_settings_button = self.title_bar.settings_button
        self.delete_task_button = _StyledButton("删除所选", variant="default")
        self.retry_task_button = _StyledButton("重试失败", variant="default")
        self.write_summary_label = QLabel("写入=0 · 跳过=0 · 总计=0")
        self.active_template_label = QLabel("当前模板")
        self.active_template_combo = QComboBox()
        self.status_badge_labels: dict[str, QLabel] = {}
        self.start_button.setObjectName("primaryButton")
        self.pause_all_button.setObjectName("secondaryButton")
        self.clear_all_button.setObjectName("secondaryButton")
        self.write_button.setObjectName("successButton")
        self.excel_path_button.setObjectName("secondaryButton")
        self.add_text_button.setObjectName("secondaryButton")
        self.add_file_button.setObjectName("secondaryButton")
        self.delete_task_button.setObjectName("secondaryButton")
        self.retry_task_button.setObjectName("secondaryButton")
        self.write_summary_label.setObjectName("metaLabel")

        # Left: task queue panel
        self.left_task_panel = QFrame()
        self.left_task_panel.setObjectName("cardPanel")
        self.tasks_table = QTableWidget(0, 4)
        self.tasks_table.setHorizontalHeaderLabels(["", "源文件", "进度", "操作"])
        self.tasks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tasks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tasks_table.setAlternatingRowColors(True)
        self.tasks_table.setItemDelegate(TaskQueueDelegate(self.tasks_table))
        self.tasks_table.setItemDelegateForColumn(2, ProgressDelegate(self.tasks_table))
        self.tasks_table.setMouseTracking(True)
        self.tasks_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tasks_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tasks_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tasks_table.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        task_header = self.tasks_table.horizontalHeader()
        task_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        task_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        task_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        task_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tasks_table.setColumnWidth(0, 32)
        self.tasks_table.setColumnWidth(2, TASK_QUEUE_PROGRESS_COLUMN_WIDTH)
        self.tasks_table.setColumnWidth(3, TASK_QUEUE_ACTIONS_COLUMN_WIDTH)
        self.task_empty_overlay = _EmptyStateOverlay(
            title="暂无任务",
            description="添加文件或文本以开始任务",
            icon_svg_path=str(_ICONS_DIR / "empty-folder-plus.svg"),
            icon_container_size=44,
            icon_border_radius=12,
            parent=self.tasks_table.viewport(),
        )

        # Center: result review table
        self.center_result_panel = QFrame()
        self.center_result_panel.setObjectName("cardPanel")
        self.results_table = QTableWidget(0, 0)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setItemDelegate(ResultTableDelegate(self.results_table))
        self.results_table.setMouseTracking(True)
        self.results_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.results_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.results_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.result_empty_overlay = _EmptyStateOverlay(
            title="暂无结果",
            description="运行任务后结果将显示在此处",
            icon_svg_path=str(_ICONS_DIR / "empty-table.svg"),
            icon_container_size=56,
            icon_border_radius=14,
            parent=self.results_table.viewport(),
        )
        self.result_count_label = QLabel("0 条")
        self.result_count_label.setObjectName("resultCountLabel")

        # Right: inspector
        self.right_inspector_panel = QFrame()
        self.right_inspector_panel.setObjectName("cardPanel")
        self.inspector_scroll_area = QScrollArea()
        self.inspector_scroll_area.setWidgetResizable(True)
        self.inspector_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.inspector_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.inspector_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.inspector_scroll_area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.inspector_content = QWidget()
        self.inspector_content.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        self.inspector_ocr_text_value = _InspectorOcrTextEdit(INSPECTOR_OCR_PLACEHOLDER)
        self.inspector_ocr_text_value.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.inspector_copy_ocr_button = QPushButton("复制")
        self.inspector_copy_ocr_button.setObjectName("inspectorCopyButton")
        self.inspector_copy_ocr_button.setToolTip("复制 OCR 文本")
        self.inspector_copy_ocr_button.setAccessibleName("复制 OCR 文本")
        self.inspector_copy_ocr_button.setEnabled(False)
        self.inspector_markdown_toggle = QPushButton("MD")
        self.inspector_markdown_toggle.setObjectName("inspectorMarkdownToggle")
        self.inspector_markdown_toggle.setCheckable(True)
        self.inspector_markdown_toggle.setEnabled(False)
        self.inspector_markdown_toggle.setToolTip("查看 Markdown OCR Text（只读）")
        self.inspector_markdown_toggle.setAccessibleName("查看 Markdown OCR Text（只读）")
        self.ocr_char_count_label = QLabel("0 / 0 字符")
        self.ocr_char_count_label.setObjectName("ocrCharCountLabel")
        self.inspector_status_badge = QLabel("空闲")
        self.inspector_status_badge.setObjectName("inspectorStatusBadge")
        self.inspector_task_id_value = QLabel("-")
        self.inspector_source_value = QLabel("-")
        self.inspector_status_value = QLabel("-")
        self.inspector_error_value = QLabel("-")
        self.inspector_retry_value = QLabel("-")
        self.inspector_source_value.setWordWrap(True)
        self.inspector_source_value.setMinimumWidth(0)
        self.inspector_source_value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # The task id is a full UUID; like the source value it must not dictate the
        # detail panel's minimum width. A single-line label keeps a minimumSizeHint
        # as wide as the whole UUID, forcing the detail panel past its 40% share and
        # collapsing the OCR text panel below its intended 60% (font-dependent, so it
        # only surfaces under some fonts). wordWrap lets the label shrink at the
        # hyphens so the 3:2 stretch holds.
        self.inspector_task_id_value.setWordWrap(True)
        self.inspector_task_id_value.setMinimumWidth(0)
        self.inspector_task_id_value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for value_label in (
            self.inspector_status_value,
            self.inspector_error_value,
            self.inspector_retry_value,
        ):
            value_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.inspector_tips_value = QLabel(INSPECTOR_EMPTY_TIPS)
        self.inspector_tips_value.setObjectName("inspectorTipsLabel")
        self.inspector_tips_value.setWordWrap(True)

        # Bottom: status console
        self.bottom_status_console = QFrame()
        self.bottom_status_console.setObjectName("consolePanel")
        self.toggle_console_button = _StyledButton("显示控制台", variant="default")
        self.toggle_console_button.setObjectName("secondaryButton")
        self.clear_console_button = _StyledButton("清空", variant="default")
        self.clear_console_button.setObjectName("secondaryButton")
        self.clear_console_button.setToolTip("清空当前控制台消息")
        self.clear_console_button.setAccessibleName("清空控制台")
        self.console_list = QListWidget()
        self.console_list.setAlternatingRowColors(True)
        self.console_hint_label = QLabel("最近日志（最多 500 条）")
        self.console_hint_label.setObjectName("metaLabel")

        self.status_dot = _StatusDot()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusBarLabel")
        self.status_detail_label = QLabel("")
        self.status_detail_label.setObjectName("statusDetailLabel")

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
        self._apply_button_icons()

    def _apply_button_icons(self) -> None:
        icon_size = QSize(14, 14)
        fg_color = COLORS["fg_secondary"]
        on_primary_color = COLORS["on_primary"]

        for button, svg_name, color in (
            (self.start_button, "play.svg", on_primary_color),
            (self.pause_all_button, "pause.svg", fg_color),
            (self.clear_all_button, "clear-all.svg", fg_color),
            (self.delete_task_button, "delete.svg", fg_color),
            (self.retry_task_button, "retry.svg", fg_color),
            (self.add_file_button, "file-plus.svg", fg_color),
            (self.add_text_button, "text-plus.svg", fg_color),
            (self.excel_path_button, "folder.svg", fg_color),
            (self.write_button, "download.svg", on_primary_color),
            (self.clear_console_button, "trash-sm.svg", fg_color),
            (self.toggle_console_button, "terminal.svg", fg_color),
        ):
            icon = self._load_svg_icon(_TOOLBAR_ICON_DIR / svg_name, color)
            button.setIcon(icon)
            button.setIconSize(icon_size)

    @staticmethod
    def _load_svg_icon(svg_path: Path, color: str | None = None) -> QIcon:
        pixmap = QPixmap(str(svg_path))
        if pixmap.isNull():
            logger.warning("toolbar icon not found: %s", svg_path)
            return QIcon()
        if color:
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor(color))
            painter.end()
        return QIcon(pixmap)

    def _build_layout(self) -> None:
        root = QWidget(self)
        root.setObjectName("mainRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 0, 8, 8)
        root_layout.setSpacing(0)
        root_layout.addWidget(self.title_bar)

        self.content_container = QWidget()
        self.content_container.setObjectName("mainContentContainer")
        self.content_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if sys.platform != "win32":
            shadow = QGraphicsDropShadowEffect(self.content_container)
            shadow.setBlurRadius(24)
            shadow.setOffset(0, 4)
            shadow.setColor(QColor(20, 20, 40, int(255 * 0.08)))
            self.content_container.setGraphicsEffect(shadow)
        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(10)

        # Top bar: single-row toolbar grouping all global task actions.
        self.top_control_bar.setFixedHeight(48)
        top_layout = QHBoxLayout(self.top_control_bar)
        top_layout.setContentsMargins(4, 0, 8, 0)
        top_layout.setSpacing(8)
        top_layout.addWidget(self.start_button)
        top_layout.addWidget(self.pause_all_button)
        top_layout.addWidget(self.clear_all_button)
        top_layout.addWidget(self._make_toolbar_separator())
        top_layout.addWidget(self.delete_task_button)
        top_layout.addWidget(self.retry_task_button)
        top_layout.addWidget(self._make_toolbar_separator())
        top_layout.addWidget(self.write_summary_label)
        self.status_badge_strip = self._build_status_badge_strip()
        top_layout.addWidget(self.status_badge_strip)
        top_layout.addWidget(self.active_template_label)
        top_layout.addWidget(self.active_template_combo)
        top_layout.addStretch(1)

        # Left panel
        left_layout = QVBoxLayout(self.left_task_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)
        task_queue_header = QHBoxLayout()
        task_queue_header.setContentsMargins(0, 0, 0, 0)
        task_queue_title = QLabel("任务队列")
        task_queue_title.setObjectName("panelTitleLabel")
        task_queue_header.addWidget(task_queue_title)
        task_queue_header.addStretch(1)
        task_queue_header.addWidget(self.add_file_button)
        task_queue_header.addWidget(self.add_text_button)
        left_layout.addLayout(task_queue_header)
        left_layout.addWidget(self.tasks_table)
        self._configure_task_queue_drop_targets()

        # Center panel
        center_layout = QVBoxLayout(self.center_result_panel)
        center_layout.setContentsMargins(10, 10, 10, 10)
        center_layout.setSpacing(8)
        result_review_header = QHBoxLayout()
        result_review_header.setContentsMargins(0, 0, 0, 0)
        result_review_title = QLabel("结果预览")
        result_review_title.setObjectName("panelTitleLabel")
        result_review_header.addWidget(result_review_title)
        result_review_header.addWidget(self.result_count_label)
        result_review_header.addStretch(1)
        result_review_header.addWidget(self.excel_path_button)
        result_review_header.addWidget(self.write_button)
        center_layout.addLayout(result_review_header)
        center_layout.addWidget(self.results_table)

        # Inspector panel, visually placed below Result Review while preserving the historical attribute name.
        right_layout = QVBoxLayout(self.right_inspector_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)
        inspector_head_layout = QHBoxLayout()
        inspector_head_layout.setContentsMargins(0, 0, 0, 0)
        inspector_head_layout.setSpacing(6)
        self.inspector_ocr_header_label = QLabel("OCR 文本")
        self.inspector_ocr_header_label.setObjectName("panelTitleLabel")
        inspector_head_layout.addWidget(self.inspector_ocr_header_label)
        inspector_head_layout.addWidget(self.inspector_copy_ocr_button)
        inspector_head_layout.addWidget(self.inspector_markdown_toggle)
        inspector_head_layout.addWidget(self.ocr_char_count_label)
        inspector_head_layout.addStretch(1)
        inspector_title = QLabel("详情")
        inspector_title.setObjectName("panelTitleLabel")
        # Center both on the row's vertical axis so they stay aligned regardless of
        # font metrics: the badge's border makes it a few pixels taller than the plain
        # title, so AlignTop would leave their centers offset by a font-dependent amount.
        inspector_head_layout.addWidget(inspector_title, alignment=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        inspector_head_layout.addWidget(self.inspector_status_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        right_layout.addLayout(inspector_head_layout)
        inspector_layout = QHBoxLayout(self.inspector_content)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(12)
        ocr_panel = QWidget()
        ocr_panel.setObjectName("inspectorSubPanel")
        ocr_layout = QVBoxLayout(ocr_panel)
        ocr_layout.setContentsMargins(0, 0, 0, 0)
        ocr_layout.setSpacing(6)
        ocr_layout.addWidget(self.inspector_ocr_text_value, stretch=1)

        detail_panel = QWidget()
        detail_panel.setObjectName("inspectorSubPanel")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)
        detail_layout.addWidget(self._inspector_row("任务编号", self.inspector_task_id_value))
        detail_layout.addWidget(self._inspector_row("来源", self.inspector_source_value))
        detail_layout.addWidget(self._inspector_row("状态", self.inspector_status_value))
        detail_layout.addWidget(self._inspector_row("错误", self.inspector_error_value))
        detail_layout.addWidget(self._inspector_row("重试", self.inspector_retry_value))
        detail_layout.addWidget(self._inspector_row("提示", self.inspector_tips_value))
        detail_layout.addStretch(1)
        inspector_layout.addWidget(ocr_panel, stretch=3)
        inspector_layout.addWidget(detail_panel, stretch=2)
        self.inspector_scroll_area.setWidget(self.inspector_content)
        right_layout.addWidget(self.inspector_scroll_area, stretch=1)

        # Bottom console (buttons moved to status bar)
        console_layout = QVBoxLayout(self.bottom_status_console)
        console_layout.setContentsMargins(10, 10, 10, 10)
        console_layout.setSpacing(8)
        console_layout.addWidget(self.console_hint_label)
        console_layout.addWidget(self.console_list)

        # Split shell
        horizontal_split = QSplitter(Qt.Orientation.Horizontal)
        horizontal_split.addWidget(self.left_task_panel)
        self.result_inspector_splitter = QSplitter(Qt.Orientation.Vertical)
        self.result_inspector_splitter.addWidget(self.center_result_panel)
        self.result_inspector_splitter.addWidget(self.right_inspector_panel)
        self.result_inspector_splitter.setStretchFactor(0, 1)
        self.result_inspector_splitter.setStretchFactor(1, 0)
        self.result_inspector_splitter.setSizes([10_000, INSPECTOR_PANEL_HEIGHT])
        right_workspace = self.result_inspector_splitter
        horizontal_split.addWidget(right_workspace)
        horizontal_split.setStretchFactor(0, 7)
        horizontal_split.setStretchFactor(1, 13)

        vertical_split = QSplitter(Qt.Orientation.Vertical)
        top_workspace = QWidget()
        top_workspace_layout = QVBoxLayout(top_workspace)
        top_workspace_layout.setContentsMargins(0, 0, 0, 0)
        top_workspace_layout.addWidget(horizontal_split)
        vertical_split.addWidget(top_workspace)
        vertical_split.addWidget(self.bottom_status_console)
        vertical_split.setStretchFactor(0, 8)
        vertical_split.setStretchFactor(1, 2)

        content_layout.addWidget(self.top_control_bar)
        content_layout.addWidget(vertical_split, stretch=1)
        status_bar_widget = QWidget()
        status_bar_widget.setObjectName("statusBarWidget")
        status_bar_widget.setFixedHeight(BOTTOM_STATUS_BAR_HEIGHT)
        status_bar_layout = QHBoxLayout(status_bar_widget)
        status_bar_layout.setContentsMargins(16, 0, 16, 0)
        status_bar_layout.setSpacing(8)
        status_bar_layout.addWidget(self.status_dot)
        status_bar_layout.addWidget(self.status_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        status_bar_layout.addWidget(self.status_detail_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        status_bar_layout.addStretch(1)
        status_bar_layout.addWidget(self.model_preload_indicator)
        status_bar_layout.addWidget(self.clear_console_button)
        status_bar_layout.addWidget(self.toggle_console_button)
        content_layout.addWidget(status_bar_widget)
        root_layout.addWidget(self.content_container, stretch=1)
        self._configure_resize_event_targets(root)

        # Shell keyboard navigation (Alt+1..4)
        QShortcut("Alt+1", self, activated=self.left_task_panel.setFocus)
        QShortcut("Alt+2", self, activated=self.center_result_panel.setFocus)
        QShortcut("Alt+3", self, activated=self.right_inspector_panel.setFocus)
        QShortcut("Alt+4", self, activated=self.bottom_status_console.setFocus)

    @staticmethod
    def _make_toolbar_separator() -> QFrame:
        sep = QFrame()
        sep.setObjectName("toolbarSeparator")
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedSize(1, 20)
        return sep

    def _build_status_badge_strip(self) -> QWidget:
        strip = QWidget()
        strip.setObjectName("statusBadgeStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)
        for key in ("total", "success", "failed", "skipped"):
            label_text, bg, border, color = STATUS_BADGE_STYLES[key]
            badge = QLabel(f"{label_text} 0")
            badge.setObjectName(f"statusBadge_{key}")
            badge.setAccessibleName(f"{label_text} 0")
            badge.setStyleSheet(
                f"background: {bg}; border: 1px solid {border}; border-radius: {RADIUS['sm']}; "
                f"padding: 0 12px; color: {color}; font-weight: {TYPOGRAPHY['micro'][1]}; "
                f"font-size: {TYPOGRAPHY['micro'][0]}; min-height: 28px; max-height: 28px;"
            )
            layout.addWidget(badge)
            self.status_badge_labels[key] = badge
        return strip

    @staticmethod
    def _inspector_row(title: str, value_widget: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"{title}:")
        label.setFixedWidth(70)
        layout.addWidget(label)
        layout.addWidget(value_widget, stretch=1)
        return row

    def _bind_events(self) -> None:
        self.add_text_button.clicked.connect(self._on_add_text)
        self.add_file_button.clicked.connect(self._on_add_file)
        self.start_button.clicked.connect(self._on_start)
        self.pause_all_button.clicked.connect(self._on_pause_all_tasks)
        self.clear_all_button.clicked.connect(self._on_clear_all_tasks)
        self.write_button.clicked.connect(self._on_write_excel)
        self.excel_path_button.clicked.connect(self._on_choose_excel_path)
        self.title_bar.settings_clicked.connect(self._on_open_settings)
        self.active_template_combo.currentIndexChanged.connect(self._on_active_template_changed)
        self.delete_task_button.clicked.connect(self._on_delete_selected_task)
        self.retry_task_button.clicked.connect(self._on_retry_selected_task)
        self.toggle_console_button.clicked.connect(self._on_toggle_console)
        self.clear_console_button.clicked.connect(self._on_clear_console)
        self.inspector_copy_ocr_button.clicked.connect(self._on_copy_inspector_ocr_text)
        self.inspector_markdown_toggle.toggled.connect(self._on_toggle_inspector_markdown)
        self.inspector_scroll_area.verticalScrollBar().rangeChanged.connect(self._on_inspector_vscroll_range_changed)
        self.console_list.itemActivated.connect(self._on_console_item_activated)
        self.console_list.itemClicked.connect(self._on_console_item_activated)
        self.tasks_table.itemSelectionChanged.connect(self._on_task_selection_changed)
        self.results_table.itemSelectionChanged.connect(self._on_result_selection_changed)
        self.results_table.itemChanged.connect(self._on_result_table_item_changed)
        self.inspector_ocr_text_value.selectionChanged.connect(self._update_ocr_char_count)

    def _configure_task_queue_drop_targets(self) -> None:
        targets = [self.left_task_panel]
        targets.extend(self.left_task_panel.findChildren(QWidget))
        self._task_queue_drop_targets = targets
        for target in targets:
            target.setAcceptDrops(True)
            target.installEventFilter(self)
        self._empty_state_hosts = {
            self.tasks_table.viewport(): self.task_empty_overlay,
            self.results_table.viewport(): self.result_empty_overlay,
        }
        for host in self._empty_state_hosts:
            host.installEventFilter(self)

    def _configure_resize_event_targets(self, root: QWidget) -> None:
        self._resize_root = root
        root.installEventFilter(self)
        handle_edges = {
            "left": ["left"],
            "right": ["right"],
            "top": ["top"],
            "bottom": ["bottom"],
            "topLeft": ["left", "top"],
            "topRight": ["right", "top"],
            "bottomLeft": ["left", "bottom"],
            "bottomRight": ["right", "bottom"],
        }
        for name, edges in handle_edges.items():
            handle = QWidget(root)
            handle.setObjectName(f"resizeHandle_{name}")
            handle.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            handle.setMouseTracking(True)
            handle.setProperty("resizeEdges", ",".join(edges))
            cursor = self._edge_cursor(edges)
            if cursor is not None:
                handle.setCursor(cursor)
            handle.installEventFilter(self)
        self._sync_resize_handles()

    def _sync_resize_handles(self) -> None:
        if not hasattr(self, "_resize_root"):
            return
        width = self._resize_root.width()
        height = self._resize_root.height()
        margin = self._RESIZE_MARGIN
        geometries = {
            "resizeHandle_left": QRect(0, margin, margin, max(0, height - margin * 2)),
            "resizeHandle_right": QRect(max(0, width - margin), margin, margin, max(0, height - margin * 2)),
            "resizeHandle_top": QRect(margin, 0, max(0, width - margin * 2), margin),
            "resizeHandle_bottom": QRect(margin, max(0, height - margin), max(0, width - margin * 2), margin),
            "resizeHandle_topLeft": QRect(0, 0, margin, margin),
            "resizeHandle_topRight": QRect(max(0, width - margin), 0, margin, margin),
            "resizeHandle_bottomLeft": QRect(0, max(0, height - margin), margin, margin),
            "resizeHandle_bottomRight": QRect(max(0, width - margin), max(0, height - margin), margin, margin),
        }
        for handle in self._resize_root.findChildren(QWidget):
            if not handle.objectName().startswith("resizeHandle_"):
                continue
            geo = geometries.get(handle.objectName())
            if geo is not None:
                handle.setGeometry(geo)
                handle.setVisible(not self.isMaximized())
                if not self.isMaximized():
                    handle.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        try:
            if watched is getattr(self, "_resize_root", None) and event.type() == QEvent.Type.Resize:
                self._sync_resize_handles()
            if isinstance(watched, QWidget) and self._resize_handle_edges(watched):
                if self._handle_resize_event_filter(watched, event):
                    return True
            if watched in getattr(self, "_task_queue_drop_targets", ()):
                if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and self._event_has_input_paths(event):
                    event.acceptProposedAction()
                    return True
                if event.type() == QEvent.Type.Drop:
                    mime_data = getattr(event, "mimeData", lambda: None)()
                    if isinstance(mime_data, QMimeData) and self._add_input_mime_data(mime_data):
                        event.acceptProposedAction()
                        return True
            overlay = getattr(self, "_empty_state_hosts", {}).get(watched)
            if overlay is not None and event.type() == QEvent.Type.Resize:
                overlay.sync_to_parent()
        except RuntimeError:
            return False
        return super().eventFilter(watched, event)

    def _handle_resize_event_filter(self, watched: QWidget, event: QEvent) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            return self._begin_resize(self._resize_handle_edges(watched), event.globalPosition().toPoint())
        if event_type == QEvent.Type.MouseMove:
            if self._resize_edges:
                self._update_resize(event.globalPosition().toPoint())
                return True
            return False
        if event_type == QEvent.Type.MouseButtonRelease and self._resize_edges:
            self._end_resize()
            return True
        return False

    @staticmethod
    def _resize_handle_edges(handle: QWidget) -> list[str]:
        value = handle.property("resizeEdges")
        if not isinstance(value, str) or not value:
            return []
        return value.split(",")

    def _detect_edge(self, pos: QPoint) -> list[str]:
        if self.isMaximized():
            return []
        edges: list[str] = []
        if pos.x() <= self._RESIZE_MARGIN:
            edges.append("left")
        if pos.x() >= self.width() - self._RESIZE_MARGIN:
            edges.append("right")
        if pos.y() <= self._RESIZE_MARGIN:
            edges.append("top")
        if pos.y() >= self.height() - self._RESIZE_MARGIN:
            edges.append("bottom")
        return edges

    def _edge_cursor(self, edges: list[str]) -> Qt.CursorShape | None:
        edge_set = set(edges)
        if edge_set in ({"left"}, {"right"}):
            return Qt.CursorShape.SizeHorCursor
        if edge_set in ({"top"}, {"bottom"}):
            return Qt.CursorShape.SizeVerCursor
        if edge_set in ({"left", "top"}, {"right", "bottom"}):
            return Qt.CursorShape.SizeFDiagCursor
        if edge_set in ({"right", "top"}, {"left", "bottom"}):
            return Qt.CursorShape.SizeBDiagCursor
        return None

    def _begin_resize(self, edges: list[str], global_pos: QPoint) -> bool:
        if self.isMaximized():
            return False
        if not edges:
            return False
        self._resize_edges = edges
        self._resize_start_pos = global_pos
        self._resize_start_geo = self.geometry()
        return True

    def _update_resize(self, global_pos: QPoint) -> None:
        if not self._resize_edges or self._resize_start_pos is None or self._resize_start_geo is None:
            return

        delta = global_pos - self._resize_start_pos
        geo = self._resize_start_geo
        x, y, width, height = geo.x(), geo.y(), geo.width(), geo.height()
        min_width, min_height = self.minimumWidth(), self.minimumHeight()

        if "right" in self._resize_edges:
            width = max(min_width, geo.width() + delta.x())
        if "bottom" in self._resize_edges:
            height = max(min_height, geo.height() + delta.y())
        if "left" in self._resize_edges:
            width = max(min_width, geo.width() - delta.x())
            x = geo.x() + geo.width() - width
        if "top" in self._resize_edges:
            height = max(min_height, geo.height() - delta.y())
            y = geo.y() + geo.height() - height

        self.setGeometry(x, y, width, height)

    def _end_resize(self) -> None:
        self._resize_edges = []
        self._resize_start_pos = None
        self._resize_start_geo = None
        self.unsetCursor()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._begin_resize(
            self._detect_edge(event.pos()), event.globalPosition().toPoint()
        ):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._resize_edges:
            self._update_resize(event.globalPosition().toPoint())
            return

        cursor = self._edge_cursor(self._detect_edge(event.pos()))
        if cursor:
            self.setCursor(cursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        self._end_resize()
        super().mouseReleaseEvent(event)

    def _is_titlebar_caption_area(self, pos: QPoint) -> bool:
        titlebar_pos = self.title_bar.mapFrom(self, pos)
        if not self.title_bar.rect().contains(titlebar_pos):
            return False
        return not self.title_bar._is_interactive_child_at(titlebar_pos)

    def _win32_hit_test_result(self, pos: QPoint) -> int:
        htclient = 1
        htcaption = 2
        htleft = 10
        htright = 11
        httop = 12
        httopleft = 13
        httopright = 14
        htbottom = 15
        htbottomleft = 16
        htbottomright = 17
        edge_set = set(self._detect_edge(pos))

        if edge_set == {"left", "top"}:
            return httopleft
        if edge_set == {"right", "top"}:
            return httopright
        if edge_set == {"left", "bottom"}:
            return htbottomleft
        if edge_set == {"right", "bottom"}:
            return htbottomright
        if edge_set == {"left"}:
            return htleft
        if edge_set == {"right"}:
            return htright
        if edge_set == {"top"}:
            return httop
        if edge_set == {"bottom"}:
            return htbottom
        if self._is_titlebar_caption_area(pos):
            return htcaption
        return htclient

    def changeEvent(self, event: Any) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_resize_handles()
            self.title_bar._update_max_button_tooltip()
            self.title_bar.max_button.update()
        super().changeEvent(event)

    def nativeEvent(self, event_type: bytes, message: int) -> tuple[bool, int]:
        if sys.platform == "win32":
            try:
                import ctypes.wintypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                wm_nccalcsize = 0x0083
                wm_nchittest = 0x0084

                if msg.message == wm_nccalcsize:
                    return True, 0

                if msg.message == wm_nchittest:
                    # lParam carries physical screen pixels; QCursor.pos() is in Qt's
                    # logical (device-independent) coordinates, the pair mapFromGlobal
                    # expects. Decoding lParam directly breaks on scaled monitors
                    # (DPR != 1), misclassifying the caption area as a resize edge.
                    pos = self.mapFromGlobal(QCursor.pos())
                    return True, self._win32_hit_test_result(pos)
            except Exception:
                pass

        return False, 0

    def _apply_style(self) -> None:
        self.setStyleSheet(generate_main_qss())

    def _set_status(self, text: str, *, detail: str = "", state: str = "ready") -> None:
        self.status_label.setText(text)
        self.status_detail_label.setText(detail)
        self.status_dot.set_state(state)

    def _on_add_text(self) -> None:
        dialog = self._create_text_paste_dialog()
        dialog.exec()

    def _create_text_paste_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("添加文本")
        dialog.resize(520, 360)
        dialog.setStyleSheet(generate_main_qss())

        root_layout = QVBoxLayout(dialog)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addStretch(1)
        confirm_button = QPushButton("确认")
        confirm_button.setObjectName("primaryButton")
        header_layout.addWidget(confirm_button, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        root_layout.addLayout(header_layout)

        text_input = QPlainTextEdit()
        text_input.setObjectName("textPasteInput")
        root_layout.addWidget(text_input, stretch=1)

        confirm_button.clicked.connect(lambda: self._confirm_text_paste(dialog, text_input))
        return dialog

    def _confirm_text_paste(self, dialog: QDialog, text_input: QPlainTextEdit) -> None:
        if self._add_text_from_paste(text_input.toPlainText()):
            dialog.accept()

    def _add_text_from_paste(self, text: str) -> bool:
        if not text.strip():
            self._set_status("文本输入为空。", state="error")
            return False
        task_id = self.controller.add_text_task(text)
        self._sync_task_review_inputs()
        self.append_log(level="info", message="文本任务已添加。", task_id=task_id)
        self._refresh_all_tables()
        if self._queue_running():
            self._set_status("文本任务已添加到运行队列，自动分派已启用。", state="ready")
        else:
            self._set_status("文本任务已添加。点击「全部开始」处理。", state="ready")
        return True

    def _on_add_file(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择输入文件",
            "",
            "支持的文件 (*.png *.jpg *.jpeg *.bmp *.tiff *.pdf);;图片文件 (*.png *.jpg *.jpeg *.bmp *.tiff);;PDF 文件 (*.pdf)",
        )
        self._add_input_files(file_paths)

    def _add_input_files(self, input_paths: list[str]) -> None:
        expanded_paths = self._expand_input_paths(input_paths)
        if not expanded_paths:
            return

        added_count = 0
        has_invalid_path = False
        has_unsupported = False
        for file_path in expanded_paths:
            try:
                normalized_path = self._normalize_ui_path(file_path)
                extension = Path(normalized_path).suffix.lower()
            except (OSError, ValueError):
                has_invalid_path = True
                self.append_log(level="warn", message="无效文件路径已拒绝。")
                continue
            if extension == ".pdf":
                if hasattr(self.controller, "add_pdf_task"):
                    task_id = self.controller.add_pdf_task(normalized_path)
                else:
                    task_id = self.controller.add_image_task(normalized_path)
                self.append_log(level="info", message="PDF 任务已添加。", task_id=task_id)
                added_count += 1
            elif extension in SUPPORTED_IMAGE_EXTENSIONS:
                task_id = self.controller.add_image_task(normalized_path)
                self.append_log(level="info", message="图片任务已添加。", task_id=task_id)
                added_count += 1
            else:
                has_unsupported = True
                self.append_log(level="warn", message="不支持的文件类型已拒绝。")

        if added_count <= 0:
            if has_invalid_path:
                self._set_status("文件路径无效，请检查后重试。", state="error")
            else:
                self._set_status("不支持的文件类型，请选择图片或 PDF。", state="error")
            return

        if has_invalid_path:
            self._set_status("部分文件因路径无效被跳过。", state="error")
        elif has_unsupported:
            self._set_status("不支持的文件类型，请选择图片或 PDF。", state="error")
        elif self._queue_running():
            self._set_status("文件已添加到运行队列，自动分派已启用。", state="ready")
        else:
            self._set_status("文件已添加。点击「全部开始」处理。", state="ready")
        self._sync_task_review_inputs()
        self._refresh_all_tables()

    def _expand_input_paths(self, input_paths: list[str]) -> list[str]:
        expanded_paths: list[str] = []
        for input_path in input_paths:
            try:
                normalized_path = self._normalize_ui_path(input_path)
            except (OSError, ValueError):
                expanded_paths.append(input_path)
                continue
            path = Path(normalized_path)
            if path.is_dir():
                expanded_paths.extend(
                    str(child)
                    for child in sorted(path.iterdir(), key=lambda item: item.name.casefold())
                    if child.is_file()
                )
            else:
                expanded_paths.append(normalized_path)
        return expanded_paths

    def _add_input_mime_data(self, mime_data: QMimeData) -> bool:
        if not mime_data.hasUrls():
            return False
        input_paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile() and url.toLocalFile()]
        if not input_paths:
            return False
        self._add_input_files(input_paths)
        return True

    @staticmethod
    def _event_has_input_paths(event: QEvent) -> bool:
        mime_data = getattr(event, "mimeData", lambda: None)()
        return isinstance(mime_data, QMimeData) and any(
            url.isLocalFile() and bool(url.toLocalFile()) for url in mime_data.urls()
        )

    def _on_start(self) -> None:
        pending_count = self._pending_count()
        paused_count = self._paused_count()
        if pending_count <= 0 and paused_count <= 0:
            self._set_status("无待处理或暂停的任务可启动。", state="idle")
            return
        if self._queue_running():
            self._set_status("识别已在运行中。", state="running")
            return
        config_ready, config_reason = self._config_ready()
        if not config_ready:
            self._set_status(config_reason, state="error")
            return
        if paused_count > 0:
            try:
                if hasattr(self.controller, "resume_all_tasks"):
                    self.controller.resume_all_tasks()
                elif hasattr(self.controller, "engine") and hasattr(self.controller.engine, "resume_all"):
                    self.controller.engine.resume_all()
                else:
                    raise RuntimeError("当前控制器不支持全部恢复。")
            except Exception as exc:
                self.append_log(level="error", message=str(exc), error_code=getattr(exc, "code", None))
                self._set_status(str(exc), state="error")
                return
            self._sync_task_review_inputs()
            self._refresh_tasks_table()
        if not self._start_queue_processing():
            self._set_status("无待处理任务可启动。", state="idle")

    def _start_queue_processing(self) -> bool:
        pending_count = self._pending_count()
        if pending_count <= 0:
            return False
        self.append_log(level="info", message=f"Queue started with {pending_count} pending tasks.")
        self._ui_recognition_running = True
        self._reset_stream_result_cache()
        self._set_status("识别运行中…", state="running")
        self._refresh_controls()
        self._start_recognition_in_background()
        return True

    def _start_recognition_in_background(self) -> None:
        thread = QThread(self)
        worker = _RecognitionWorker(controller=self.controller)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.engine_event.connect(self._on_engine_event)
        worker.finished.connect(self._on_recognition_finished)
        worker.failed.connect(self._on_recognition_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._on_recognition_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._recognition_thread = thread
        self._recognition_worker = worker
        thread.start()

    def _on_recognition_thread_finished(self) -> None:
        self._recognition_worker = None
        self._recognition_thread = None

    def start_model_preload(self) -> None:
        """窗口显示后触发本地模型后台预热（仅 ocr_service 提供该能力时）。"""
        # 重入守卫：预热线程仍在运行时直接返回，避免覆盖 _model_preload_thread 引用，
        # 否则旧 QThread 失去引用、_shutdown_model_preload 无法 quit/wait，运行中被销毁
        # 会触发 Qt abort（与 _shutdown_recognition 防范的崩溃面同源）。
        if self._model_preload_thread is not None and self._model_preload_thread.isRunning():
            return
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

    def closeEvent(self, event: Any) -> None:
        # 关闭时若后台识别线程仍在跑，必须先请求停止并等它结束，否则运行中的 QThread
        # 被销毁会触发 Qt abort（SIGABRT）。在线 OCR 的长轮询会拉长这个窗口。
        self._shutdown_recognition()
        self._shutdown_model_preload()
        super().closeEvent(event)

    def _shutdown_recognition(self) -> None:
        thread = self._recognition_thread
        if thread is None or not thread.isRunning():
            return
        request_stop = getattr(self.controller, "request_stop", None)
        if callable(request_stop):
            # 设置 stop_event：在线轮询经 cancel_check 中止，各阶段循环退出，worker.run() 返回。
            request_stop()
        thread.quit()
        # 等待线程优雅结束（在线轮询在 cancel 后约一个 poll_interval 内返回）；
        # 超时则放行关闭，避免 UI 永久卡死。
        # 残留窗口（已知、低概率）：cancel 只在轮询循环边界生效，无法中断**进行中**的
        # requests 调用；若关闭恰逢一次阻塞的 submit/get（socket timeout 可达数十秒），
        # 15s wait 会超时返回、线程随窗口销毁仍可能 abort。彻底消除需可中断 socket，
        # 成本高且概率极低，此处接受该残留（主崩溃面——任意运行中识别——已消除）。
        thread.wait(15000)

    def _shutdown_model_preload(self) -> None:
        thread = self._model_preload_thread
        if thread is None or not thread.isRunning():
            return
        # 模型加载是不可中断的同步调用，无法提前 cancel；quit 仅停事件循环，
        # wait 会阻塞到 worker.run() 返回（最坏约一次模型加载耗时）。
        thread.quit()
        thread.wait(15000)

    def _on_recognition_finished(self) -> None:
        self._ui_recognition_running = False
        self._reset_stream_result_cache()
        self._sync_task_review_inputs()
        self._refresh_all_tables()
        self._set_status("识别已完成。", state="ready")
        failed_count = self._failed_count()
        if failed_count > 0:
            self.append_log(level="warn", message=f"{failed_count} tasks failed. You can retry them.")

    def _on_recognition_failed(self, message: str) -> None:
        self._ui_recognition_running = False
        self._reset_stream_result_cache()
        self._sync_task_review_inputs()
        self._refresh_all_tables()
        self.append_log(level="error", message=f"Recognition failed: {message}")
        self._set_status("识别失败。", state="error")

    def _on_engine_event(self, event: EngineEvent) -> None:
        self._task_review_coordinator.handle_engine_event(event)
        self._sync_task_review_inputs()
        match event:
            case TaskAutoDispatchTriggered(task_id=task_id):
                if task_id:
                    self.append_log(level="info", message="任务已从运行队列自动分派。", task_id=task_id)
                self._refresh_tasks_table()
            case TaskOcrCompleted(task_id=task_id):
                if task_id and event.active_template_name:
                    self.append_log(
                        level="info",
                        message=f"Active template: {event.active_template_name}.",
                        task_id=task_id,
                    )
                if task_id and event.region_rescue:
                    # 只统计 OCR 区域重抢救;attribution_correction(归属对调)语义不同,不计入。
                    rescue_items = [
                        item
                        for item in event.region_rescue
                        if item.get("kind") != "attribution_correction"
                    ]
                    if rescue_items:
                        rescued = sum(1 for item in rescue_items if item.get("success") is True)
                        self.append_log(
                            level="info",
                            message=f"Region rescue completed. success={rescued}, total={len(rescue_items)}.",
                            task_id=task_id,
                        )
                self._refresh_tasks_table()
            case TaskPageResultStreamed(task_id=task_id):
                self._render_stream_results_table()
                self._refresh_tasks_table()
                total = len(self.controller.tasks) if hasattr(self.controller, "tasks") else 0
                if self._ui_recognition_running and total > 0:
                    self._set_status("识别运行中…", state="running")
            case TaskStarted():
                self._refresh_tasks_table()
            case TaskOcrProgressed():
                # 在线 OCR 整份 job 轮询期的页级进度心跳：刷新任务表让进度“动起来”。
                self._refresh_tasks_table()
            case TaskProgressed(task_id=task_id) | TaskSucceeded(task_id=task_id) | TaskFailed(task_id=task_id):
                if not task_id:
                    return
                self._render_stream_results_table()
                self._refresh_tasks_table()
                completed = self._task_review_projection.stream_task_count()
                total = len(self.controller.tasks) if hasattr(self.controller, "tasks") else 0
                if self._ui_recognition_running and total > 0:
                    self._set_status("识别运行中…", detail=f"{completed}/{total}", state="running")

    def _on_write_excel(self) -> None:
        export_rows = self._task_review_coordinator.export_rows()
        write_count, skip_count, total_count = self._write_summary_counts()
        if write_count <= 0:
            self._set_status("未选择要写入的行。", state="error")
            return

        configured_default = self._configured_output_path()
        display_path = configured_default or _DEFAULT_EXCEL_FALLBACK
        payload: dict[str, int | str] = {
            "write_count": write_count,
            "skip_count": skip_count,
            "total_count": total_count,
            "target_path": display_path,
        }
        if not self.confirm_write_fn(payload):
            self._set_status("写入已取消。", state="ready")
            return

        if hasattr(self.controller, "write_result_review_rows"):
            summary = self.controller.write_result_review_rows(export_rows, configured_default)
        else:
            summary = self.controller.write_excel(configured_default)
        self.append_log(
            level="info",
            message=f"Excel write done. written={summary.written_rows}, skipped={summary.skipped_rows}",
        )
        self._set_status(
            "写入完成。",
            detail=f"写入={summary.written_rows} · 跳过={summary.skipped_rows} · 输出={summary.output_path}",
            state="ready",
        )
        self._persist_effective_output_path(summary.output_path)
        self._refresh_excel_path_button_tooltip()

    def _on_choose_excel_path(self) -> None:
        current = self._configured_output_path() or _DEFAULT_EXCEL_FALLBACK
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "选择 Excel 输出路径",
            current,
            "Excel 文件 (*.xlsx)",
        )
        chosen = (chosen or "").strip()
        if not chosen:
            return
        chosen = str(Path(chosen).with_suffix(".xlsx"))
        if not hasattr(self.controller, "engine") or not hasattr(self.controller.engine, "config"):
            self._set_status("引擎未就绪，无法保存 Excel 路径。", state="error")
            return
        self.controller.engine.config.default_excel_path = chosen
        if self.settings_controller is not None and hasattr(self.settings_controller, "save_config"):
            try:
                self.settings_controller.save_config(self.controller.engine.config)
            except Exception:
                self.append_log(level="warn", message="Excel 路径未能保存到配置。")
        self._refresh_excel_path_button_tooltip()
        self._set_status(f"Excel 路径已设置为 {chosen}", state="ready")

    def _refresh_excel_path_button_tooltip(self) -> None:
        configured = self._configured_output_path()
        effective = configured or _DEFAULT_EXCEL_FALLBACK
        self.excel_path_button.setToolTip(f"当前 Excel 路径：{effective}")

    def _default_confirm_write(self, payload: dict[str, int | str]) -> bool:
        platform_name = QApplication.platformName().lower()
        if "offscreen" in platform_name:
            return True

        message = (
            f"将 {payload['write_count']} 行写入 {payload['target_path']}？\n"
            f"（跳过={payload['skip_count']}，总计={payload['total_count']}）"
        )
        result = QMessageBox.question(self, "确认写入", message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return result == QMessageBox.StandardButton.Yes

    def _on_open_settings(self) -> None:
        if self.settings_controller is None:
            self._set_status("设置控制器不可用。", state="error")
            return
        dialog = SettingsDialog(controller=self.settings_controller, parent=self)
        accepted = dialog.exec()
        if accepted:
            try:
                latest_config = self.settings_controller.load_config()
                if hasattr(self.controller, "engine"):
                    self.controller.engine.config = project_active_template_config(latest_config)
                    ocr_service = getattr(self.controller.engine, "ocr_service", None)
                    if ocr_service is not None and hasattr(ocr_service, "update_runtime_options"):
                        options_factory = getattr(ocr_service, "runtime_options_from_app_config", None)
                        if callable(options_factory):
                            ocr_service.update_runtime_options(options_factory(self.controller.engine.config))
                self.append_log(level="info", message="设置已保存。")
                self._set_status("设置已保存。", state="ready")
                self._refresh_active_template_combo()
                self._refresh_results_table()
            except Exception as exc:  # pragma: no cover
                QMessageBox.warning(self, "设置", f"重新加载配置失败：{exc}")

    def _refresh_active_template_combo(self) -> None:
        if not hasattr(self.controller, "engine") or not hasattr(self.controller.engine, "config"):
            return
        self.active_template_combo.blockSignals(True)
        self.active_template_combo.clear()
        cfg = self.controller.engine.config
        if not hasattr(cfg, "templates") or not hasattr(cfg, "active_template_id"):
            self.active_template_combo.blockSignals(False)
            return
        cfg = project_active_template_config(cfg)
        catalog = TemplateCatalog.load(cfg.templates, cfg.active_template_id)
        for entry in catalog.list_entries():
            self.active_template_combo.addItem(entry.name, entry.id)
        index = self.active_template_combo.findData(catalog.active_id)
        if index >= 0:
            self.active_template_combo.setCurrentIndex(index)
        self.active_template_combo.blockSignals(False)

    def _on_active_template_changed(self) -> None:
        if not hasattr(self.controller, "engine") or not hasattr(self.controller.engine, "config"):
            return
        template_id = self.active_template_combo.currentData()
        if template_id is None:
            return
        cfg = self.controller.engine.config
        if not hasattr(cfg, "templates") or not hasattr(cfg, "active_template_id"):
            return
        cfg.active_template_id = str(template_id)
        self.controller.engine.config = project_active_template_config(cfg)
        if self.settings_controller is not None and hasattr(self.settings_controller, "save_config"):
            try:
                self.settings_controller.save_config(self.controller.engine.config)
            except Exception as exc:
                self.append_log(level="warn", message="当前模板未能保存到配置。")
                QMessageBox.warning(self, "设置", f"保存当前模板失败：{exc}")

    def _selected_task_id(self) -> str | None:
        node = self._selected_task_node()
        if node is None:
            return None
        task_id = node.get("task_id")
        return str(task_id).strip() if task_id is not None else None

    def _selected_task_page_index(self) -> int | None:
        node = self._selected_task_node()
        if node is None:
            return None
        return self._coerce_page_index(node.get("page_index"))

    def _selected_result_payload(self) -> dict[str, object] | None:
        selected_ranges = self.results_table.selectedRanges()
        if not selected_ranges:
            return None
        selected_row_index = selected_ranges[0].topRow()
        if 0 <= selected_row_index < len(self._rendered_result_rows):
            return dict(self._rendered_result_rows[selected_row_index])
        item = self.results_table.item(selected_row_index, 1)
        if item is None:
            return None
        task_id = item.text().strip()
        return {"task_id": task_id} if task_id else None

    def _selected_result_task_id(self) -> str | None:
        selection_data = self._selected_result_payload()
        if selection_data is None:
            return None
        task_id = selection_data.get("task_id")
        return str(task_id).strip() if task_id is not None else None

    def _selected_result_page_index(self) -> int | None:
        selection_data = self._selected_result_payload()
        if selection_data is None:
            return None
        return self._coerce_page_index(selection_data.get("page_index"))

    def _task_by_id(self, task_id: str) -> Any | None:
        for task in self.controller.tasks:
            if task.task_id == task_id:
                return task
        return None

    def _task_review_item_results(self) -> list[Any]:
        if hasattr(self.controller, "engine"):
            engine = getattr(self.controller, "engine")
            return list(getattr(engine, "item_results", []) or [])
        if hasattr(self.controller, "item_results"):
            return list(getattr(self.controller, "item_results", []) or [])
        return []

    def _sync_task_review_inputs(self) -> None:
        self._task_review_coordinator.reconcile_all(
            tasks=list(self.controller.tasks),
            result_rows=list(getattr(self.controller, "result_rows", []) or []),
            item_results=self._task_review_item_results(),
        )

    def _task_review_snapshot(self):
        return self._task_review_coordinator.snapshot()

    @staticmethod
    def _coerce_page_index(raw_value: object) -> int | None:
        if isinstance(raw_value, int):
            return raw_value if raw_value > 0 else None
        if isinstance(raw_value, str) and raw_value.strip().isdigit():
            value = int(raw_value.strip())
            return value if value > 0 else None
        return None

    def _selected_task_node(self) -> dict[str, object] | None:
        selected_ranges = self.tasks_table.selectedRanges()
        if not selected_ranges:
            return None
        row_index = selected_ranges[0].topRow()
        if 0 <= row_index < len(self._task_table_nodes):
            return dict(self._task_table_nodes[row_index])
        item = self.tasks_table.item(row_index, 0)
        if item is None:
            return None
        return {"node_type": "task", "task_id": item.text(), "page_index": None}

    @staticmethod
    def _normalize_ui_path(raw_path: str) -> str:
        return str(Path(str(raw_path)).expanduser())

    @staticmethod
    def _display_task_status(status: str) -> str:
        mapping = {
            "pending": "等待中",
            "paused": "已暂停",
            "running_ocr": "OCR中",
            "running_extract": "抽取中",
            "done": "已完成",
            "success": "已完成",
            "completed": "已完成",
            "failed": "失败",
            "empty": "跳过",
        }
        return mapping.get(status, status)

    @staticmethod
    def _task_progress_status_text(status: str) -> str:
        mapping = {
            "pending": "等待中",
            "paused": "已暂停",
            "running_ocr": "OCR中",
            "running_extract": "抽取中",
            "done": "已完成",
            "success": "已完成",
            "completed": "已完成",
            "failed": "失败",
            "empty": "跳过",
            "Pending": "等待中",
            "Paused": "已暂停",
            "Running OCR": "OCR中",
            "Running Extract": "抽取中",
            "Done": "已完成",
            "Failed": "失败",
            "Skipped": "跳过",
        }
        return mapping.get(status, status)

    @staticmethod
    def _clamp_progress(progress: object) -> int:
        try:
            value = int(float(progress))
        except (TypeError, ValueError):
            value = 0
        return max(0, min(100, value))

    @classmethod
    def _task_progress_cell_text(cls, status: str, progress: object) -> str:
        percent = 100 if status == "done" else cls._clamp_progress(progress)
        return f"{cls._task_progress_status_text(status)}\n{percent}%"

    @staticmethod
    def _task_source_full_value(task: Any) -> str:
        return str(getattr(task, "source_path", "") or getattr(task, "source_value", "") or "")

    @classmethod
    def _task_source_display_text(cls, task: Any) -> str:
        source_type = str(getattr(task, "source_type", "") or "")
        full_value = cls._task_source_full_value(task)
        if source_type == "text":
            body = full_value
            if len(body) > TASK_SOURCE_DISPLAY_PREFIX_CHARS:
                return f"文本:{body[:TASK_SOURCE_DISPLAY_PREFIX_CHARS]}…"
            return f"文本:{body}"

        basename = Path(full_value).name or full_value
        path_value = Path(basename)
        suffix = path_value.suffix
        stem = basename[: -len(suffix)] if suffix else basename
        if len(stem) > TASK_SOURCE_DISPLAY_PREFIX_CHARS:
            return f"{stem[:TASK_SOURCE_DISPLAY_PREFIX_CHARS]}…{suffix}"
        return basename

    @staticmethod
    def _make_table_item(text: str, *, payload: dict[str, object] | None = None, tooltip: str | None = None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if payload is not None:
            item.setData(Qt.ItemDataRole.UserRole, dict(payload))
        if tooltip is not None:
            item.setToolTip(tooltip)
        return item

    def _task_page_count(self, task: Any) -> int:
        return self._task_review_projection.task_page_count(task)

    def _task_has_page_children(self, task: Any) -> bool:
        return self._task_review_projection.task_has_page_children(task)

    def _task_is_collapsed(self, task_id: str) -> bool:
        return self._task_review_projection.is_task_collapsed(task_id)

    def _task_page_snapshot(self, task: Any, page_index: int) -> Any | None:
        for snapshot in list(getattr(task, "pdf_page_ocr_snapshots", []) or []):
            if self._coerce_page_index(getattr(snapshot, "page_index", None)) == page_index:
                return snapshot
        return None

    def _task_page_result(self, task: Any, page_index: int) -> Any | None:
        for page_result in list(getattr(task, "pdf_page_results", []) or []):
            if self._coerce_page_index(getattr(page_result, "page_index", None)) == page_index:
                return page_result
        return None

    def _task_page_exists(self, task: Any, page_index: int) -> bool:
        return 1 <= page_index <= max(self._task_page_count(task), 0)

    def _task_page_status_payload(self, task: Any, page_index: int) -> tuple[str, str, str]:
        page_result = self._task_page_result(task, page_index)
        if page_result is not None:
            status_value = str(getattr(page_result, "status", "") or "")
            display_status = self._display_task_status(status_value)
            error_code = str(getattr(page_result, "error_code", "") or "")
            progress_value = "100"
            return display_status, progress_value, error_code

        if self._task_page_snapshot(task, page_index) is not None:
            status_value = "running_extract" if str(getattr(task, "status", "")) == "running_extract" else "pending"
            return self._display_task_status(status_value), "0", ""

        return self._display_task_status("pending"), "0", ""

    def _stream_rows_from_extract_rows(
        self,
        *,
        task_id: str,
        rows: list[ExtractRow],
        default_page_index: int | None = None,
    ) -> list[dict[str, object]]:
        normalized_rows: list[dict[str, object]] = []
        for row in rows:
            normalized_rows.append(
                {
                    "row_id": row.row_id,
                    "task_id": row.task_id or task_id,
                    "values": list(row.values),
                    "action": row.action,
                    "page_index": row.page_index if row.page_index is not None else default_page_index,
                    "ocr_confidence": row.ocr_confidence,
                    "is_error_row": row.is_error_row,
                }
            )
        return normalized_rows

    def _set_label_text_if_changed(self, label: QLabel, value: str) -> None:
        if label.text() != value:
            label.setText(value)
        if not label.wordWrap():
            label.setMinimumWidth(label.sizeHint().width())

    def _set_ocr_text_if_changed(self, value: str) -> None:
        if self.inspector_ocr_text_value.text() != value:
            self.inspector_ocr_text_value.setText(value)
        self._refresh_inspector_copy_button_state()

    def _update_inspector_content_width(self) -> None:
        layout = self.inspector_content.layout()
        if layout is None:
            return
        viewport_width = self.inspector_scroll_area.viewport().width()
        self.inspector_content.setMinimumWidth(max(viewport_width, layout.minimumSize().width()))
        self.inspector_content.adjustSize()

    def _current_ocr_text(self) -> str:
        return self.inspector_ocr_text_value.text().strip()

    def _has_copyable_ocr_text(self, text_value: str | None = None) -> bool:
        effective_text = text_value if text_value is not None else self._current_ocr_text()
        return bool(effective_text and effective_text != INSPECTOR_OCR_PLACEHOLDER)

    def _refresh_inspector_copy_button_state(self) -> None:
        self.inspector_copy_ocr_button.setEnabled(self._has_copyable_ocr_text())
        self._update_ocr_char_count()

    def _update_ocr_char_count(self) -> None:
        total_text = self.inspector_ocr_text_value.text()
        if total_text == INSPECTOR_OCR_PLACEHOLDER:
            self.ocr_char_count_label.setText("0 / 0 字符")
            return
        total_len = len(total_text)
        cursor = self.inspector_ocr_text_value.textCursor()
        selected_len = len(cursor.selectedText()) if cursor.hasSelection() else total_len
        self.ocr_char_count_label.setText(f"{selected_len} / {total_len} 字符")

    def _on_toggle_inspector_markdown(self, checked: bool) -> None:
        self._render_inspector_ocr_text()

    def _render_inspector_ocr_text(self) -> None:
        data = self._last_inspector_data
        markdown = data.markdown_text if data is not None else None
        self.inspector_markdown_toggle.setEnabled(bool(markdown))
        if not markdown and self.inspector_markdown_toggle.isChecked():
            signals_blocked = self.inspector_markdown_toggle.blockSignals(True)
            self.inspector_markdown_toggle.setChecked(False)
            self.inspector_markdown_toggle.blockSignals(signals_blocked)
        if self.inspector_markdown_toggle.isChecked() and markdown:
            self._set_ocr_text_if_changed(markdown)
            return
        self._set_ocr_text_if_changed(data.ocr_text if data is not None else "")

    def _on_copy_inspector_ocr_text(self) -> None:
        text_value = self._current_ocr_text()
        if not self._has_copyable_ocr_text(text_value):
            self.inspector_copy_ocr_button.setEnabled(False)
            self._set_status("无 OCR 文本可复制。", state="error")
            return
        QApplication.clipboard().setText(text_value)
        self._set_status("OCR 文本已复制。", state="ready")

    def _on_inspector_vscroll_range_changed(self) -> None:
        # Force content width to match viewport when scrollbar appears/disappears.
        content = self.inspector_scroll_area.widget()
        if content is None:
            return
        viewport_width = self.inspector_scroll_area.viewport().width()
        if viewport_width > 0 and content.width() != viewport_width:
            content.setFixedWidth(viewport_width)

    def _reset_inspector_scroll_to_top(self) -> None:
        scroll_bar = self.inspector_scroll_area.verticalScrollBar()
        if scroll_bar.value() != scroll_bar.minimum():
            scroll_bar.setValue(scroll_bar.minimum())

    def _inspector_tip_for_status(self, status: str) -> str:
        if status == "failed":
            return "建议：修复配置或输入后，使用「重试失败」重试该任务。"
        if status == "pending":
            return "建议：点击全部开始开始识别。"
        if status == "paused":
            return "建议：点击全部开始或行内继续恢复任务处理。"
        if status == "done":
            return "建议：在结果表审核 action 后执行「写入 Excel」。"
        return "任务处理中，请观察状态与日志。"

    def _resolve_ocr_text(self, task_id: str, page_index: int | None = None) -> str:
        self._task_review_projection.select_task(task_id, page_index=page_index)
        return self._task_review_snapshot().inspector.ocr_text

    def _render_inspector_task(self, task_id: str | None, page_index: int | None = None) -> None:
        if task_id is not None:
            self._task_review_projection.select_task(task_id, page_index=page_index)
        self._apply_inspector_render_data(self._task_review_snapshot().inspector)

    def _apply_inspector_render_data(self, inspector: InspectorRenderData) -> None:
        self._last_inspector_data = inspector
        self._render_inspector_ocr_text()
        self._set_label_text_if_changed(self.inspector_task_id_value, inspector.task_id_text)
        self._set_label_text_if_changed(self.inspector_source_value, inspector.source_text)
        self.inspector_source_value.setToolTip(inspector.source_text)
        self._set_label_text_if_changed(self.inspector_status_value, inspector.status_text)
        self._set_label_text_if_changed(self.inspector_error_value, inspector.error_text)
        self._set_label_text_if_changed(self.inspector_retry_value, inspector.retry_text)
        self._set_label_text_if_changed(self.inspector_tips_value, inspector.tips_text)
        self._update_inspector_status_badge(inspector.status_text)
        self._update_inspector_content_width()

    def _update_inspector_status_badge(self, status: str) -> None:
        text, bg, border, fg = self._INSPECTOR_BADGE_MAP.get(status, ("空闲", "#FFFFFF", "#E0E0E5", "#73737A"))
        self.inspector_status_badge.setText(text)
        self.inspector_status_badge.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; color: {fg}; "
            f"font-family: {TYPOGRAPHY['mono']}; font-size: 10px; "
            f"padding: 2px 8px; border-radius: {RADIUS['full']};"
        )

    def _select_task_row_silently(self, task_id: str | None, page_index: int | None = None) -> None:
        self._inspector_syncing_selection = True
        try:
            if task_id is None:
                self.tasks_table.clearSelection()
                return
            fallback_parent_row: int | None = None
            for row_index, node in enumerate(self._task_table_nodes):
                node_task_id = str(node.get("task_id", "") or "")
                node_page_index = self._coerce_page_index(node.get("page_index"))
                if node_task_id != task_id:
                    continue
                if node_page_index is None and fallback_parent_row is None:
                    fallback_parent_row = row_index
                if page_index is not None and node_page_index == page_index:
                    self.tasks_table.selectRow(row_index)
                    return
                if page_index is None and node_page_index is None:
                    self.tasks_table.selectRow(row_index)
                    return
            if fallback_parent_row is not None:
                self.tasks_table.selectRow(fallback_parent_row)
                return
            self.tasks_table.clearSelection()
        finally:
            self._inspector_syncing_selection = False

    def _repair_inspector_selection(self) -> None:
        previous_selection = (self._inspector_selected_task_id, self._inspector_selected_page_index)
        snapshot = self._task_review_snapshot()
        selected_task_id = snapshot.inspector.task_id
        selected_page_index = snapshot.inspector.page_index
        self._inspector_selected_task_id = selected_task_id
        self._inspector_selected_page_index = selected_page_index
        self._select_task_row_silently(selected_task_id, selected_page_index)
        self._apply_inspector_render_data(snapshot.inspector)
        if (selected_task_id, selected_page_index) != previous_selection:
            self._reset_inspector_scroll_to_top()

    def _pin_inspector_task(self, task_id: str, *, page_index: int | None = None, sync_task_row: bool) -> None:
        if self._task_by_id(task_id) is None:
            return
        previous_selection = (self._inspector_selected_task_id, self._inspector_selected_page_index)
        was_collapsed = self._task_is_collapsed(task_id)
        self._task_review_projection.select_task(task_id, page_index=page_index)
        if sync_task_row:
            if was_collapsed and not self._task_is_collapsed(task_id):
                self._refresh_tasks_table()
                return
            self._select_task_row_silently(task_id, page_index)
        snapshot = self._task_review_snapshot()
        self._inspector_selected_task_id = snapshot.inspector.task_id
        self._inspector_selected_page_index = snapshot.inspector.page_index
        self._apply_inspector_render_data(snapshot.inspector)
        current_selection = (snapshot.inspector.task_id, snapshot.inspector.page_index)
        if current_selection != previous_selection:
            self._reset_inspector_scroll_to_top()

    def _on_pause_all_tasks(self) -> None:
        try:
            if hasattr(self.controller, "pause_all_tasks"):
                self.controller.pause_all_tasks()
            elif hasattr(self.controller, "engine") and hasattr(self.controller.engine, "pause_all"):
                self.controller.engine.pause_all()
            else:
                raise RuntimeError("当前控制器不支持全部暂停。")
            self._sync_task_review_inputs()
            self.append_log(level="info", message="所有待处理任务已暂停。")
            self._refresh_all_tables()
            self._set_status("所有待处理任务已暂停。", state="ready")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _on_clear_all_tasks(self) -> None:
        if not self._can_clear_all():
            message = "当前任务状态不满足“全部清空”条件。仅当全部处于 paused 或全部处于 done 时，才可清空。"
            QMessageBox.warning(self, "无法清空", message)
            self._set_status("无法清空：需要所有任务均已暂停或已完成。", state="error")
            return
        try:
            if hasattr(self.controller, "clear_all_tasks"):
                self.controller.clear_all_tasks()
            elif hasattr(self.controller, "engine") and hasattr(self.controller.engine, "clear_all"):
                self.controller.engine.clear_all()
            else:
                raise RuntimeError("当前控制器不支持全部清空。")
            self._task_review_coordinator.apply_queue_cleared()
            self._sync_task_review_inputs()
            self.append_log(level="info", message="所有任务已清空。")
            self._refresh_all_tables()
            self._set_status("所有任务已清空。", state="idle")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _on_pause_task(self, task_id: str) -> None:
        try:
            if hasattr(self.controller, "pause_task"):
                self.controller.pause_task(task_id)
            elif hasattr(self.controller, "engine") and hasattr(self.controller.engine, "pause_task"):
                self.controller.engine.pause_task(task_id)
            else:
                raise RuntimeError("当前控制器不支持暂停任务。")
            self._sync_task_review_inputs()
            self.append_log(level="info", message="任务已暂停。", task_id=task_id)
            self._refresh_all_tables()
            self._set_status("任务已暂停。", state="ready")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), task_id=task_id, error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _on_resume_task(self, task_id: str) -> None:
        try:
            if hasattr(self.controller, "resume_task"):
                self.controller.resume_task(task_id)
            elif hasattr(self.controller, "engine") and hasattr(self.controller.engine, "resume_task"):
                self.controller.engine.resume_task(task_id)
            else:
                raise RuntimeError("当前控制器不支持恢复任务。")
            self._sync_task_review_inputs()
            self.append_log(level="info", message="任务已恢复。", task_id=task_id)
            self._refresh_all_tables()
            if self._queue_running():
                self._set_status("任务已恢复。", state="ready")
                return
            config_ready, config_reason = self._config_ready()
            if not config_ready:
                self._set_status(config_reason, state="error")
                return
            if self._start_queue_processing():
                return
            self._set_status("任务已恢复。", state="ready")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), task_id=task_id, error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _on_delete_task(self, task_id: str) -> None:
        try:
            self.controller.delete_task(task_id)
            self._task_review_coordinator.apply_task_deleted(task_id)
            self._sync_task_review_inputs()
            self.append_log(level="info", message="任务已删除。", task_id=task_id)
            self._refresh_all_tables()
            self._set_status("任务已删除。", state="ready")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), task_id=task_id, error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _on_confirm_delete_task(self, task_id: str, display_source: str = "") -> None:
        target = display_source or task_id
        result = QMessageBox.question(
            self,
            "确认删除",
            f"删除任务 {target}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self._on_delete_task(task_id)

    def _on_delete_selected_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            self._set_status("请选择要删除的任务。", state="error")
            return
        self._on_delete_task(task_id)

    def _on_retry_selected_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            self._set_status("请选择要重试的任务。", state="error")
            return
        try:
            self.controller.retry_task(task_id)
            self._task_review_coordinator.apply_task_retried(task_id)
            self._sync_task_review_inputs()
            self.append_log(level="info", message="任务已重置为暂停。", task_id=task_id)
            self._refresh_all_tables()
            self._set_status("任务已标记重试。", state="ready")
        except Exception as exc:
            self.append_log(level="error", message=str(exc), task_id=task_id, error_code=getattr(exc, "code", None))
            self._set_status(str(exc), state="error")

    def _refresh_all_tables(self) -> None:
        self._sync_task_review_inputs()
        self._refresh_tasks_table(sync_task_review_inputs=False)
        self._refresh_results_table(sync_task_review_inputs=False)
        self._refresh_controls()
        self._refresh_status_badges()

    def _refresh_tasks_table(self, *, sync_task_review_inputs: bool = True) -> None:
        if sync_task_review_inputs:
            self._sync_task_review_inputs()
        snapshot = self._task_review_snapshot()
        self._task_table_nodes = [row.payload for row in snapshot.task_queue_rows]
        self.tasks_table.setRowCount(len(snapshot.task_queue_rows))
        for row_index, row in enumerate(snapshot.task_queue_rows):
            node = row.payload
            task_id = row.task_id
            task = self._task_by_id(task_id)
            if task is None:
                continue

            page_index = row.page_index
            source_text = row.source_text
            progress_text = row.progress_text
            is_page_row = row.is_page_row

            expand_item = self._make_table_item(
                "",
                payload=node,
                tooltip=task.task_id if not is_page_row else f"{task.task_id} | 第 {page_index} 页",
            )
            self.tasks_table.setItem(row_index, 0, expand_item)

            source_item = self._make_table_item(source_text, payload=node, tooltip=row.source_tooltip)
            self.tasks_table.setItem(row_index, 1, source_item)

            progress_item = self._make_table_item(progress_text, payload=node, tooltip=progress_text)
            if is_page_row and page_index is not None:
                _display_status, progress_value, _error_code = self._task_page_status_payload(task, page_index)
            else:
                progress_value = getattr(task, "progress", 0)
            progress_item.setData(PROGRESS_ROLE, self._clamp_progress(progress_value) / 100.0)
            progress_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tasks_table.setItem(row_index, 2, progress_item)

            actions_item = self._make_table_item("", payload=node)
            self.tasks_table.setItem(row_index, 3, actions_item)

            if is_page_row:
                self._apply_page_row_font(expand_item, source_item, progress_item, actions_item)
                self.tasks_table.setCellWidget(row_index, 0, self._build_blank_actions_widget())
                self.tasks_table.setCellWidget(row_index, 3, self._build_blank_actions_widget())
                continue

            if self._task_has_page_children(task):
                self.tasks_table.setCellWidget(row_index, 0, self._build_task_expand_widget(task_id))
            else:
                self.tasks_table.setCellWidget(row_index, 0, self._build_blank_actions_widget())
            self.tasks_table.setCellWidget(row_index, 3, self._build_task_actions_widget(task_id, str(task.status), source_text))
        self.tasks_table.resizeRowsToContents()
        self._refresh_controls()
        self._repair_inspector_selection()
        self._sync_task_empty_state()
        self._refresh_status_badges()

    def _build_task_expand_widget(self, task_id: str) -> QWidget:
        collapsed = self._task_is_collapsed(task_id)
        widget = self._build_task_table_cell_widget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        button = QPushButton()
        button.setObjectName("task_row_expand_button")
        icon_path = _TASK_QUEUE_PDF_COLLAPSED_ICON if collapsed else _TASK_QUEUE_PDF_EXPANDED_ICON
        button.setIcon(QIcon(str(icon_path)))
        button.setIconSize(QSize(16, 16))
        button.setToolTip("展开页面" if collapsed else "折叠页面")
        button.setAccessibleName("展开 PDF 页面" if collapsed else "折叠 PDF 页面")
        button.setFixedSize(24, 24)
        button.clicked.connect(lambda _checked=False, current_id=task_id: self._on_toggle_task_pages(current_id))
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)
        return widget

    def _build_task_actions_widget(self, task_id: str, status_value: str, display_source: str) -> QWidget:
        actions_widget = self._build_task_table_cell_widget()
        actions_layout = QHBoxLayout(actions_widget)
        actions_layout.setContentsMargins(1, 2, 1, 2)
        actions_layout.setSpacing(2)
        actions_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        control_button = QPushButton()
        control_button.setObjectName("task_row_control_button")
        control_button.setFixedSize(TASK_QUEUE_ACTION_BUTTON_SIZE, TASK_QUEUE_ACTION_BUTTON_SIZE)
        control_button.setIconSize(QSize(14, 14))
        if status_value == "paused":
            control_button.setIcon(QIcon(str(_TASK_QUEUE_ACTION_RESUME_ICON)))
            control_button.setToolTip("继续任务")
            control_button.setAccessibleName("继续任务")
            control_button.setEnabled(True)
            control_button.clicked.connect(lambda _checked=False, current_id=task_id: self._on_resume_task(current_id))
        else:
            control_button.setIcon(QIcon(str(_TASK_QUEUE_ACTION_PAUSE_ICON)))
            control_button.setToolTip("暂停任务" if status_value == "pending" else "当前状态不可暂停")
            control_button.setAccessibleName("暂停任务")
            control_button.setEnabled(status_value == "pending")
            control_button.clicked.connect(lambda _checked=False, current_id=task_id: self._on_pause_task(current_id))

        delete_button = QPushButton()
        delete_button.setObjectName("task_row_delete_button")
        delete_button.setFixedSize(TASK_QUEUE_ACTION_BUTTON_SIZE, TASK_QUEUE_ACTION_BUTTON_SIZE)
        delete_button.setIconSize(QSize(14, 14))
        delete_button.setIcon(QIcon(str(_TASK_QUEUE_ACTION_DELETE_ICON)))
        delete_button.setToolTip("删除任务")
        delete_button.setAccessibleName("删除任务")
        delete_button.setEnabled(not status_value.startswith("running_"))
        delete_button.clicked.connect(
            lambda _checked=False, current_id=task_id, current_source=display_source: self._on_confirm_delete_task(
                current_id, current_source
            )
        )
        actions_layout.addWidget(control_button, alignment=Qt.AlignmentFlag.AlignCenter)
        actions_layout.addWidget(delete_button, alignment=Qt.AlignmentFlag.AlignCenter)
        return actions_widget

    def _apply_page_row_font(self, *items: QTableWidgetItem) -> None:
        base_font = items[0].font() if items else self.tasks_table.font()
        child_font = QFont(base_font)
        point_size = child_font.pointSizeF()
        if point_size <= 0:
            point_size = 12.0
        child_font.setPointSizeF(max(8.0, point_size - 1.0))
        for item in items:
            item.setFont(child_font)

    @staticmethod
    def _build_blank_actions_widget() -> QWidget:
        actions_widget = MainWindow._build_task_table_cell_widget()
        actions_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        actions_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        actions_layout = QHBoxLayout(actions_widget)
        actions_layout.setContentsMargins(2, 2, 2, 2)
        actions_layout.setSpacing(4)
        return actions_widget

    @staticmethod
    def _build_task_table_cell_widget() -> QWidget:
        widget = QWidget()
        widget.setObjectName("taskTableCellWidget")
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        widget.setAutoFillBackground(False)
        return widget

    def _on_toggle_task_pages(self, task_id: str) -> None:
        self._task_review_projection.toggle_pdf_task(task_id)
        self._refresh_tasks_table()

    def _column_schema(self) -> list[str]:
        if hasattr(self.controller, "engine") and getattr(self.controller.engine, "config", None):
            examples = getattr(self.controller.engine.config, "examples_normalized", [])
            if examples and isinstance(examples[0], list) and len(examples[0]) > 0:
                return [str(col) if str(col).strip() else f"Field {idx + 1}" for idx, col in enumerate(examples[0])]
        return ["Field 1", "Field 2"]

    def _refresh_results_table(self, *, sync_task_review_inputs: bool = True) -> None:
        if sync_task_review_inputs:
            self._sync_task_review_inputs()
        snapshot = self._task_review_snapshot()
        self._render_results_table([row.payload for row in snapshot.result_review_rows], enable_action_change=True)
        self._repair_inspector_selection()

    def _render_stream_results_table(self, *, sync_task_review_inputs: bool = True) -> None:
        if sync_task_review_inputs:
            self._sync_task_review_inputs()
        snapshot = self._task_review_snapshot()
        self._render_results_table([row.payload for row in snapshot.result_review_rows], enable_action_change=False)
        self._repair_inspector_selection()

    def _render_results_table(self, rows: list[dict[str, object]], *, enable_action_change: bool) -> None:
        schema = self._column_schema()
        self._current_schema = schema
        self._rendered_result_rows = [dict(row) for row in rows]
        previous_signal_state = self.results_table.blockSignals(True)
        try:
            self.results_table.clearContents()
            self.results_table.setColumnCount(3 + len(schema))
            self.results_table.setHorizontalHeaderLabels(["选择", "任务编号", "OCR 置信度"] + schema)
            self.results_table.setColumnWidth(0, 64)
            self.results_table.setRowCount(len(rows))

            for row_index, item in enumerate(rows):
                is_error_row = bool(item.get("is_error_row", False))
                action_value = str(item.get("action", "write"))
                if action_value not in {"write", "skip"}:
                    action_value = "write"
                row_payload = {
                    "task_id": str(item.get("task_id") or ""),
                    "page_index": self._coerce_page_index(item.get("page_index")),
                    "row_id": item.get("row_id"),
                }
                selection_item = self._build_result_selection_item(
                    checked=action_value == "write" and not is_error_row,
                    enabled=enable_action_change and not is_error_row,
                )
                selection_item.setData(Qt.ItemDataRole.UserRole, row_payload)
                self.results_table.setItem(row_index, 0, selection_item)

                confidence_raw = item.get("ocr_confidence")
                if is_error_row:
                    confidence = "error"
                elif isinstance(confidence_raw, (int, float)):
                    confidence = f"{float(confidence_raw):.4f}"
                else:
                    confidence = "N/A"
                task_id_raw = item.get("task_id")
                task_id = str(task_id_raw) if task_id_raw is not None else ""
                task_id_item = QTableWidgetItem(task_id)
                task_id_item.setToolTip(task_id)
                task_id_item.setData(Qt.ItemDataRole.UserRole, row_payload)
                self.results_table.setItem(row_index, 1, task_id_item)

                confidence_item = QTableWidgetItem(confidence)
                confidence_item.setToolTip(confidence)
                self.results_table.setItem(row_index, 2, confidence_item)

                values_raw = item.get("values")
                values = list(values_raw) if isinstance(values_raw, list) else []
                for col_index in range(len(schema)):
                    if col_index < len(values):
                        value = str(values[col_index])
                    elif is_error_row:
                        value = "error"
                    else:
                        value = " "
                    value_item = QTableWidgetItem(value)
                    value_item.setToolTip(value)
                    value_item.setData(Qt.ItemDataRole.UserRole, value)
                    self.results_table.setItem(row_index, 3 + col_index, value_item)
        finally:
            self.results_table.blockSignals(previous_signal_state)
        self._refresh_write_summary_for_payload(rows)
        self.result_count_label.setText(f"{self.results_table.rowCount()} 条")
        self._sync_result_empty_state()
        self._refresh_status_badges(rendered_rows=rows)

    @staticmethod
    def _build_result_selection_item(*, checked: bool, enabled: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        if enabled:
            flags |= Qt.ItemFlag.ItemIsEnabled
        item.setFlags(flags)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip("选择写入 Excel")
        return item

    def _refresh_write_summary_for_payload(self, rows: list[dict[str, object]]) -> None:
        total = len(rows)
        write_count = sum(1 for row in rows if str(row.get("action", "write")) == "write")
        skip_count = total - write_count
        self.write_summary_label.setText(f"写入={write_count} · 跳过={skip_count} · 总计={total}")
        self._refresh_status_badges(rendered_rows=rows)

    def _reset_stream_result_cache(self) -> None:
        self._task_review_coordinator.clear_transient_result_review_state()

    def _on_result_action_changed(self, row_id: str | None, value: str) -> None:
        if not row_id:
            return
        self._task_review_coordinator.set_result_action(row_id, value)
        self._refresh_results_table()
        self._refresh_controls()

    def _on_result_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        if not bool(item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        action = "write" if item.checkState() == Qt.CheckState.Checked else "skip"
        payload = item.data(Qt.ItemDataRole.UserRole) or {}
        row_id_raw = payload.get("row_id") if isinstance(payload, dict) else None
        row_id = str(row_id_raw) if row_id_raw is not None else None
        self._on_result_action_changed(row_id, action)

    def _write_summary_counts(self) -> tuple[int, int, int]:
        exportable_rows = self._task_review_snapshot().result_review_rows
        total = len(exportable_rows)
        write_count = sum(1 for row in exportable_rows if row.action == "write" and not row.is_error_row)
        skip_count = total - write_count
        return write_count, skip_count, total

    def _configured_output_path(self) -> str:
        if hasattr(self.controller, "engine") and hasattr(self.controller.engine, "config"):
            return str(getattr(self.controller.engine.config, "default_excel_path", "") or "").strip()
        return ""

    def _persist_effective_output_path(self, effective_path: str) -> None:
        if not effective_path:
            return
        if not hasattr(self.controller, "engine") or not hasattr(self.controller.engine, "config"):
            return

        cfg = self.controller.engine.config
        cfg.default_excel_path = effective_path
        if self.settings_controller is None:
            return
        if not hasattr(self.settings_controller, "save_config"):
            return
        try:
            self.settings_controller.save_config(cfg)
        except Exception:
            # Writing to Excel is already successful; config persistence should not break UX.
            self.append_log(level="warn", message="写入已完成，但默认输出路径未能保存。")

    def _refresh_write_summary(self) -> None:
        write_count, skip_count, total = self._write_summary_counts()
        self.write_summary_label.setText(f"写入={write_count} · 跳过={skip_count} · 总计={total}")
        self._refresh_status_badges()

    def _sync_task_empty_state(self) -> None:
        self._set_empty_overlay_visible(self.task_empty_overlay, self.tasks_table.rowCount() == 0)

    def _sync_result_empty_state(self) -> None:
        row_count = self.results_table.rowCount()
        self._set_empty_overlay_visible(self.result_empty_overlay, row_count == 0)
        self.result_count_label.setText(f"{row_count} 条")

    @staticmethod
    def _set_empty_overlay_visible(overlay: _EmptyStateOverlay, visible: bool) -> None:
        overlay.sync_to_parent()
        overlay.setVisible(visible)
        if visible:
            overlay.raise_()

    def _refresh_status_badges(self, rendered_rows: list[dict[str, object]] | None = None) -> None:
        tasks = list(self.controller.tasks)
        success_statuses = {"done", "completed", "success"}
        counts = {
            "total": len(tasks),
            "success": sum(1 for task in tasks if str(task.status) in success_statuses),
            "failed": self._failed_count(),
            "skipped": self._skipped_rows_count(rendered_rows),
        }
        for key, count in counts.items():
            badge = self.status_badge_labels.get(key)
            if badge is None:
                continue
            label_text = STATUS_BADGE_STYLES[key][0]
            text = f"{label_text} {count}"
            badge.setText(text)
            badge.setAccessibleName(text)

    def _skipped_rows_count(self, rendered_rows: list[dict[str, object]] | None = None) -> int:
        if rendered_rows is not None:
            return sum(1 for row in rendered_rows if str(row.get("action", "write")) == "skip")
        if self._ui_recognition_running and self._rendered_result_rows:
            return sum(1 for row in self._rendered_result_rows if str(row.get("action", "write")) == "skip")
        return sum(1 for row in self._task_review_snapshot().result_review_rows if str(row.action) == "skip")

    def _pending_count(self) -> int:
        return sum(1 for task in self.controller.tasks if task.status == "pending")

    def _failed_count(self) -> int:
        return sum(1 for task in self.controller.tasks if task.status == "failed")

    def _paused_count(self) -> int:
        return sum(1 for task in self.controller.tasks if task.status == "paused")

    def _can_clear_all(self) -> bool:
        tasks = list(self.controller.tasks)
        if not tasks:
            return False
        statuses = {str(task.status) for task in tasks}
        return statuses == {"paused"} or statuses == {"done"}

    def _queue_running(self) -> bool:
        if self._ui_recognition_running:
            return True
        if hasattr(self.controller, "engine") and hasattr(self.controller.engine, "state"):
            return str(self.controller.engine.state) == "running"
        return any(str(task.status).startswith("running_") for task in self.controller.tasks)

    def _refresh_controls(self) -> None:
        pending_count = self._pending_count()
        paused_count = self._paused_count()
        queue_running = self._queue_running()
        write_count, _, _ = self._write_summary_counts()
        config_ready, config_reason = self._config_ready()

        self.start_button.setEnabled((pending_count > 0 or paused_count > 0) and not queue_running and config_ready)
        self.pause_all_button.setEnabled(pending_count > 0)
        self.clear_all_button.setEnabled(len(self.controller.tasks) > 0)
        self.start_button.setToolTip("" if config_ready else config_reason)
        self.write_button.setEnabled(write_count > 0 and not queue_running)
        self.retry_task_button.setEnabled(self._failed_count() > 0 and not queue_running)
        self.open_settings_button.setEnabled(not queue_running)
        self.excel_path_button.setEnabled(not queue_running)
        self._refresh_excel_path_button_tooltip()

    def _config_ready(self) -> tuple[bool, str]:
        if not hasattr(self.controller, "engine") or not hasattr(self.controller.engine, "config"):
            return True, ""
        cfg = self.controller.engine.config
        if not getattr(cfg, "model", "").strip():
            return False, "请在设置中填写模型。"
        if not getattr(cfg, "base_url", "").strip():
            return False, "请在设置中填写 Base URL。"
        if not getattr(cfg, "prompts", "").strip():
            return False, "请在设置中填写 prompts。"
        examples = getattr(cfg, "examples_normalized", [])
        if not examples or not isinstance(examples, list):
            return False, "请先配置 examples。"
        if not isinstance(examples[0], list) or len(examples[0]) < 2:
            return False, "Examples 至少需要定义 2 列。"
        provider = getattr(cfg, "provider", "")
        if provider == "openai_compatible" and not getattr(cfg, "api_key", "").strip():
            return False, "OpenAI-compatible provider 需要 API key。"
        if int(getattr(cfg, "pdf_max_pages", 30)) <= 0:
            return False, "PDF max pages 必须大于 0。"
        if int(getattr(cfg, "pdf_max_file_size", 20 * 1024 * 1024)) <= 0:
            return False, "PDF max file size 必须大于 0。"
        if int(getattr(cfg, "pdf_render_dpi", 200)) < 72:
            return False, "PDF render DPI 必须大于等于 72。"
        return True, ""

    def _on_task_selection_changed(self) -> None:
        if self._inspector_syncing_selection:
            return
        task_id = self._selected_task_id()
        if task_id is None:
            return
        self._pin_inspector_task(task_id, page_index=self._selected_task_page_index(), sync_task_row=False)

    def _on_result_selection_changed(self) -> None:
        if self._inspector_syncing_selection:
            return
        payload = self._selected_result_payload()
        if payload is None:
            return
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id:
            return
        page_index = self._coerce_page_index(payload.get("page_index"))
        row_id_raw = payload.get("row_id")
        row_id = str(row_id_raw) if row_id_raw is not None else None
        previous_selection = (self._inspector_selected_task_id, self._inspector_selected_page_index)
        was_collapsed = self._task_is_collapsed(task_id)
        self._task_review_projection.select_result(task_id=task_id, row_id=row_id, page_index=page_index)
        if was_collapsed and not self._task_is_collapsed(task_id):
            self._refresh_tasks_table()
            return
        snapshot = self._task_review_snapshot()
        self._inspector_selected_task_id = snapshot.inspector.task_id
        self._inspector_selected_page_index = snapshot.inspector.page_index
        self._select_task_row_silently(snapshot.inspector.task_id, snapshot.inspector.page_index)
        self._apply_inspector_render_data(snapshot.inspector)
        if (snapshot.inspector.task_id, snapshot.inspector.page_index) != previous_selection:
            self._reset_inspector_scroll_to_top()

    def _select_task_by_id(self, task_id: str) -> None:
        self._select_task_row_silently(task_id)

    def append_log(self, *, level: str, message: str, task_id: str | None = None, error_code: str | None = None) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{timestamp}] [{level.upper()}]"
        parts = [prefix, message]
        if error_code:
            parts.append(f"code={error_code}")
        if task_id:
            parts.append(f"task={task_id}")
        text = " ".join(parts)

        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, {"task_id": task_id, "error_code": error_code, "level": level})
        self.console_list.addItem(item)
        while self.console_list.count() > 500:
            self.console_list.takeItem(0)
        self.clear_console_button.setEnabled(self.console_list.count() > 0)

        if level.lower() == "error" and not self._console_auto_expanded_once:
            self._console_auto_expanded_once = True
            self._set_console_visible(True)

        if hasattr(self.controller, "log_event"):
            self.controller.log_event(
                "EVT-UI-001",
                {
                    "level": level,
                    "task_id": task_id,
                    "error_code": error_code,
                },
            )

    def _on_console_item_activated(self, item: QListWidgetItem) -> None:
        item_data = item.data(Qt.ItemDataRole.UserRole) or {}
        task_id = item_data.get("task_id")
        if task_id:
            self._select_task_by_id(str(task_id))
            self._on_task_selection_changed()

    def _set_console_visible(self, visible: bool) -> None:
        self.bottom_status_console.setVisible(visible)
        self.console_list.setVisible(visible)
        self.console_hint_label.setVisible(visible)
        self.toggle_console_button.setText("隐藏控制台" if visible else "显示控制台")
        self.toggle_console_button.setAccessibleName("隐藏控制台" if visible else "显示控制台")
        self.clear_console_button.setEnabled(self.console_list.count() > 0)

    def _on_toggle_console(self) -> None:
        self._set_console_visible(self.console_list.isHidden())

    def _on_clear_console(self) -> None:
        self.console_list.clear()
        self.clear_console_button.setEnabled(False)
