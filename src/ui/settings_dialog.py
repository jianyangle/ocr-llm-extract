from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.schemas import AppConfig
from src.extract.connection_check import ConnectionCheckResult, check_connection
from src.extract.model_fetcher import ModelFetchResult, fetch_models as _fetch_models
from src.extract.provider_catalog import (
    PROVIDER_PLATFORM_IDS,
    ProviderCatalogEntry,
    catalog_default_profiles,
    get_provider_catalog,
    get_provider_entry,
    profiles_as_dict,
    runtime_provider_for_platform,
)
from src.extract.errors import ExtractServiceError
from src.extract.example_parser import format_examples
from src.extract.template_catalog import (
    BUILTIN_INVOICE_ID,
    BUILTIN_SIGCARD_ID,
    TemplateCatalog,
    TemplateCatalogEntry,
    project_active_template_config,
)
from src.io.config_store import ConfigStore
from src.ocr.online_catalog import (
    DEFAULT_ONLINE_OCR_PLATFORM_ID,
    catalog_default_online_profiles,
    get_online_ocr_catalog,
    get_online_ocr_entry,
)
from src.ocr.online_connection_check import check_online_ocr_connection
from src.ui.icon_loader import load_icon
from src.ui.settings_validation import SettingsFormData, build_validated_config
from src.ui.theme import COLORS, generate_settings_qss


def _assets_dir() -> Path:
    # Frozen builds add src/ui/assets via PyInstaller --add-data, landing it under
    # _MEIPASS; in dev it sits next to this module. Mirrors icon_loader/theme.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "src" / "ui" / "assets"
    return Path(__file__).resolve().parent / "assets"


class _ConnectionCheckSignals(QObject):
    finished = Signal(object)  # ConnectionCheckResult


class _ConnectionCheckRunnable(QRunnable):
    def __init__(self, check_callable: Callable[[], ConnectionCheckResult]) -> None:
        super().__init__()
        self._check_callable = check_callable
        self.signals = _ConnectionCheckSignals()

    def run(self) -> None:
        try:
            result = self._check_callable()
        except Exception as exc:  # pragma: no cover - defensive
            result = ConnectionCheckResult(ok=False, detail=f"检测出错: {exc}")
        self.signals.finished.emit(result)


class _ModelFetchSignals(QObject):
    finished = Signal(str, object)  # platform_id, ModelFetchResult


class _ModelFetchRunnable(QRunnable):
    def __init__(
        self,
        controller,
        *,
        platform_id: str,
        entry,
        base_url: str,
        api_key: str,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._platform_id = platform_id
        self._entry = entry
        self._base_url = base_url
        self._api_key = api_key
        self.signals = _ModelFetchSignals()

    def run(self) -> None:
        try:
            result = self._controller.fetch_models(
                entry=self._entry,
                base_url=self._base_url,
                api_key=self._api_key,
            )
        except Exception as exc:
            result = ModelFetchResult(ok=False, models=(), error=f"获取模型列表出错: {exc}")
        self.signals.finished.emit(self._platform_id, result)


_LAYOUT_PARSER_OPTIONS = (
    ("单行模式", "single_line"),
    ("多栏文章", "multi_para"),
    ("保留原始顺序", "none"),
)


class SettingsController:
    def __init__(self, *, config_store: ConfigStore) -> None:
        self.config_store = config_store

    def load_config(self) -> AppConfig:
        return self.config_store.load()

    def save_config(self, config: AppConfig) -> None:
        self.config_store.save(config)

    def format_examples(self, raw_examples: str) -> list[list[str]]:
        return format_examples(raw_examples)

    def test_connection(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str = "",
        http_request=None,
    ) -> ConnectionCheckResult:
        return check_connection(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            http_request=http_request,
        )

    def test_ocr_connection(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "",
        http_post: Callable[..., Any] | None = None,
    ) -> ConnectionCheckResult:
        return check_online_ocr_connection(
            base_url=base_url,
            api_key=api_key,
            model=model,
            http_post=http_post,
        )

    def fetch_models(
        self,
        *,
        entry: ProviderCatalogEntry,
        base_url: str,
        api_key: str,
        http_get=None,
    ) -> ModelFetchResult:
        return _fetch_models(entry=entry, base_url=base_url, api_key=api_key, http_get=http_get)


class SettingsDialog(QDialog):
    def __init__(self, *, controller: SettingsController | Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._api_key_visible = False
        self._ocr_api_key_visible = False
        self._loaded_config: AppConfig | None = None
        self._provider_profile_drafts: dict[str, dict[str, str]] = {}
        self._provider_catalog = get_provider_catalog()
        self._online_ocr_catalog = get_online_ocr_catalog()
        self._online_ocr_profile_drafts: dict[str, dict[str, str]] = {}
        self.editing_online_ocr_platform_id = DEFAULT_ONLINE_OCR_PLATFORM_ID
        self._loading_online_ocr_profile = False
        self._ocr_connection_check_generation = 0
        self._active_ocr_connection_checks: set[_ConnectionCheckRunnable] = set()
        self.current_provider_platform_id = "custom"
        self.editing_provider_platform_id = "custom"
        self._remote_models_by_platform: dict[str, tuple[str, ...]] = {}
        self._fetching_models_platform_id: str | None = None
        self._active_model_fetches: set[_ModelFetchRunnable] = set()
        self._connection_check_generation = 0
        self._active_connection_checks: set[_ConnectionCheckRunnable] = set()
        self._spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spinner_index = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(100)
        self._spinner_timer.timeout.connect(self._on_spinner_tick)
        self._loading_provider_profile = False
        self._layout_parser_user_edited = False
        self._syncing_template_fields = False
        self._catalog = TemplateCatalog.load([], BUILTIN_INVOICE_ID)
        self._current_template_id: str | None = None
        self._pending_active_template_id: str = BUILTIN_INVOICE_ID
        self._template_drafts: dict[str, dict[str, object]] = {}

        self.setWindowTitle("设置")
        self.resize(960, 640)
        self.setObjectName("settingsDialog")

        self.base_url_input = QLineEdit()
        self.model_input = QComboBox()
        self.model_input.setEditable(True)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.toggle_api_key_button = QToolButton()
        self.toggle_api_key_button.setAutoRaise(True)
        self.toggle_api_key_button.setFixedSize(28, 28)
        self.toggle_api_key_button.setAccessibleName("显示或隐藏 API Key")
        self.ocr_toggle_api_key_button = QToolButton()
        self.ocr_toggle_api_key_button.setAutoRaise(True)
        self.ocr_toggle_api_key_button.setFixedSize(28, 28)
        self.ocr_toggle_api_key_button.setAccessibleName("显示或隐藏 API Key")
        self.test_connection_button = QPushButton("✓ 检测连接")
        self.test_connection_button.setObjectName("secondaryButton")
        self.set_active_provider_button = QPushButton("设为当前模型")
        self.set_active_provider_button.setObjectName("secondaryButton")
        self.ocr_use_online_checkbox = QCheckBox("启用在线OCR（离线已内置PaddleOCR Mobile模型）")
        self.ocr_provider_list = QListWidget()
        self.ocr_provider_list.setObjectName("providerList")
        self.ocr_provider_list.setFixedWidth(230)
        self.ocr_base_url_input = QLineEdit()
        self.ocr_api_key_input = QLineEdit()
        self.ocr_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ocr_model_input = QComboBox()
        self.ocr_model_input.setEditable(True)
        self.ocr_test_connection_button = QPushButton("✓ 检测连接")
        self.ocr_test_connection_button.setObjectName("secondaryButton")
        self.ocr_online_table_checkbox = QCheckBox("表格识别 (useTableRecognition)")
        self.ocr_online_formula_checkbox = QCheckBox("公式识别 (useFormulaRecognition)")
        self.ocr_online_chart_checkbox = QCheckBox("图表识别 (useChartRecognition)")
        self.ocr_online_seal_checkbox = QCheckBox("印章识别 (useSealRecognition)")

        self.ollama_num_ctx_label = QLabel("Ollama 上下文容量(num_ctx)")
        self.ollama_num_ctx_input = QSpinBox()
        self.ollama_num_ctx_input.setRange(2048, 32768)
        self.ollama_num_ctx_input.setSingleStep(1024)
        self.allow_thinking_checkbox = QCheckBox("允许模型深度思考")
        self.allow_thinking_hint = QLabel("深度思考会显著增加抽取耗时，一般信息抽取任务无需开启。")
        self.allow_thinking_hint.setObjectName("statusLabel")
        self.allow_thinking_hint.setWordWrap(True)
        self.allow_thinking_hint.setVisible(False)
        self.layout_parser_combo = QComboBox()
        for label, value in _LAYOUT_PARSER_OPTIONS:
            self.layout_parser_combo.addItem(label, value)
        self.layout_parser_online_hint = QLabel("启用在线 OCR 时，排版由在线模型决定，此设置不生效。")
        self.layout_parser_online_hint.setWordWrap(True)
        self.layout_parser_online_hint.setObjectName("statusLabel")
        self.layout_parser_online_hint.setVisible(False)
        self.grounding_mode_combo = QComboBox()
        self.grounding_mode_combo.addItem("关闭", "off")
        self.grounding_mode_combo.addItem("平衡", "balanced")
        self.grounding_mode_combo.addItem("严格", "strict")
        self.strategy_help_label = QLabel("高级参数可通过用户主目录下的 .ocr_extract_app/config.json 调整。")
        self.strategy_help_label.setWordWrap(True)
        self.strategy_help_label.setObjectName("statusLabel")

        self.template_list = QListWidget()
        self.template_list.setObjectName("templateList")
        self.template_name_input = QLineEdit()
        self.template_prompts_input = QPlainTextEdit()
        self.template_examples_input = QPlainTextEdit()
        self.new_template_button = QPushButton("新建模板")
        self.delete_template_button = QPushButton("删除")
        self.reset_template_button = QPushButton("重置为默认")
        self.set_active_template_button = QPushButton("设为当前模板")
        self.set_active_template_button.setObjectName("secondaryButton")
        self.format_button = QPushButton("格式化示例")
        self.save_button = QPushButton("✓ 校验并保存")
        self.cancel_button = QPushButton("取消")
        self.bottom_bar_status_label = QLabel("已载入配置 · config.json")
        self.bottom_bar_status_label.setObjectName("bottomBarStatus")
        self.bottom_bar_provider_icon_label = QLabel()
        self.bottom_bar_provider_icon_label.setFixedSize(16, 16)
        self.bottom_bar_provider_name_label = QLabel("")
        self.bottom_bar_provider_name_label.setObjectName("bottomBarProviderName")
        self.bottom_bar_model_label = QLabel("")
        self.bottom_bar_model_label.setObjectName("bottomBarCurrentUse")
        self.bottom_bar_template_label = QLabel("")
        self.bottom_bar_template_label.setObjectName("bottomBarCurrentUse")
        self._bottom_bar_status_generation = 0
        self.validation_error_label = QLabel("")
        self.validation_error_label.setObjectName("validation_error_label")
        self.validation_error_label.setWordWrap(True)
        self.validation_error_label.setVisible(False)
        self.save_button.setObjectName("primaryButton")
        self.format_button.setObjectName("secondaryButton")
        self.cancel_button.setObjectName("cancelButton")
        self.delete_template_button.setObjectName("secondaryButton")
        self.reset_template_button.setObjectName("secondaryButton")
        self.new_template_button.setObjectName("secondaryButton")

        self._build_layout()
        self._bind_events()
        self._apply_style()
        self._load_from_controller()

    def _build_layout(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        shell = QHBoxLayout()
        shell.setSpacing(12)
        self.settings_nav = QListWidget()
        self.settings_nav.setObjectName("settingsNav")
        self.settings_nav.setFixedWidth(150)
        self.settings_nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for label in ("抽取模型", "OCR模型", "抽取模板", "业务设置"):
            self.settings_nav.addItem(QListWidgetItem(label))
        self.settings_stack = QStackedWidget()

        self.model_service_page = self._build_model_service_page()
        self.ocr_model_page = self._build_ocr_model_page()
        self.template_page = self._build_template_page()
        self.business_settings_page = self._build_business_settings_page()
        self.settings_stack.addWidget(self.model_service_page)
        self.settings_stack.addWidget(self.ocr_model_page)
        self.settings_stack.addWidget(self.template_page)
        self.settings_stack.addWidget(self.business_settings_page)
        self.settings_nav.setCurrentRow(0)

        shell.addWidget(self.settings_nav)
        shell.addWidget(self.settings_stack, stretch=1)
        main_layout.addLayout(shell, stretch=1)
        main_layout.addWidget(self.validation_error_label)
        main_layout.addWidget(self._build_bottom_bar())

    def _build_bottom_bar(self) -> QFrame:
        self.settings_bottom_bar = QFrame()
        self.settings_bottom_bar.setObjectName("settingsBottomBar")
        layout = QHBoxLayout(self.settings_bottom_bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(10)

        current_use = QHBoxLayout()
        current_use.setSpacing(6)
        current_use.addWidget(self.bottom_bar_provider_icon_label)
        current_use.addWidget(self.bottom_bar_provider_name_label)
        for separator, label in (
            ("·", self.bottom_bar_model_label),
            ("·", self.bottom_bar_template_label),
        ):
            separator_label = QLabel(separator)
            separator_label.setObjectName("bottomBarCurrentUse")
            current_use.addWidget(separator_label)
            current_use.addWidget(label)

        information = QVBoxLayout()
        information.setSpacing(2)
        information.addLayout(current_use)
        information.addWidget(self.bottom_bar_status_label)
        layout.addLayout(information)
        layout.addStretch(1)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.save_button)
        return self.settings_bottom_bar

    def _build_model_service_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 左侧：Provider 列表
        provider_list_panel = QWidget()
        provider_list_layout = QVBoxLayout(provider_list_panel)
        provider_list_layout.setContentsMargins(0, 0, 0, 0)
        provider_list_layout.setSpacing(8)
        title_row = QHBoxLayout()
        provider_title = QLabel("抽取模型")
        provider_title.setObjectName("settingsSectionHeading")
        provider_count = QLabel(f"· {len(self._provider_catalog)}")
        provider_count.setObjectName("settingsCaption")
        title_row.addWidget(provider_title)
        title_row.addWidget(provider_count)
        title_row.addStretch(1)
        self.provider_list = QListWidget()
        self.provider_list.setObjectName("providerList")
        self.provider_list.setFixedWidth(230)
        provider_list_layout.addLayout(title_row)
        provider_list_layout.addWidget(self.provider_list, stretch=1)

        # 右侧：Provider 详情区（三张卡片）
        self.provider_detail_panel = QWidget()
        detail_layout = QVBoxLayout(self.provider_detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)

        header_card = QFrame()
        header_card.setObjectName("settingsCard")
        header_card_layout = QHBoxLayout(header_card)
        header_card_layout.setContentsMargins(14, 14, 14, 14)
        self.provider_logo_label = QLabel()
        self.provider_logo_label.setFixedSize(32, 32)
        self.provider_name_label = QLabel("")
        self.provider_subtitle_label = QLabel("")
        self.provider_subtitle_label.setObjectName("providerSubtitle")
        provider_identity = QVBoxLayout()
        provider_identity.setSpacing(2)
        provider_identity.addWidget(self.provider_name_label)
        provider_identity.addWidget(self.provider_subtitle_label)
        self.provider_website_button = QToolButton()
        self.provider_website_button.setAutoRaise(True)
        self.provider_website_button.setIcon(self._external_link_icon())
        self.provider_website_button.setToolTip("官网")
        self.provider_api_key_button = QToolButton()
        self.provider_api_key_button.setAutoRaise(True)
        self.provider_api_key_button.setIcon(self._external_link_icon())
        self.provider_api_key_button.setToolTip("API Key")
        self.provider_models_button = QToolButton()
        self.provider_models_button.setAutoRaise(True)
        self.provider_models_button.setIcon(self._external_link_icon())
        self.provider_models_button.setToolTip("模型文档")
        header_card_layout.addWidget(self.provider_logo_label)
        header_card_layout.addLayout(provider_identity)
        header_card_layout.addStretch(1)
        header_card_layout.addWidget(self.provider_website_button)
        header_card_layout.addWidget(self.provider_api_key_button)
        header_card_layout.addWidget(self.provider_models_button)

        config_card = QFrame()
        config_card.setObjectName("settingsCard")
        config_card_layout = QVBoxLayout(config_card)
        config_card_layout.setContentsMargins(14, 14, 14, 14)
        config_card_layout.setSpacing(10)
        config_heading = QLabel("连接配置")
        config_heading.setObjectName("settingsSectionHeading")
        config_card_layout.addWidget(config_heading)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.addRow("API Key", self._build_api_key_row())
        form.addRow("API 地址", self.base_url_input)
        form.addRow("模型", self.model_input)
        form.addRow(self.ollama_num_ctx_label, self.ollama_num_ctx_input)
        form.addRow("", self.allow_thinking_checkbox)
        form.addRow("", self.allow_thinking_hint)
        config_card_layout.addLayout(form)

        action_card = QFrame()
        action_card.setObjectName("settingsCard")
        action_card_layout = QVBoxLayout(action_card)
        action_card_layout.setContentsMargins(14, 14, 14, 14)
        action_card_layout.setSpacing(8)
        model_tools = QHBoxLayout()
        self.fetch_models_button = QPushButton("获取模型列表")
        model_tools.addWidget(self.fetch_models_button)
        model_tools.addWidget(self.test_connection_button)
        model_tools.addWidget(self.set_active_provider_button)
        model_tools.addStretch(1)
        action_card_layout.addLayout(model_tools)
        self.provider_fetch_error_label = QLabel("")
        self.provider_fetch_error_label.setObjectName("validation_error_label")
        self.provider_fetch_error_label.setWordWrap(True)
        self.provider_fetch_error_label.setVisible(False)
        action_card_layout.addWidget(self.provider_fetch_error_label)

        detail_layout.addWidget(header_card)
        detail_layout.addWidget(config_card)
        detail_layout.addWidget(action_card)
        detail_layout.addStretch(1)

        layout.addWidget(provider_list_panel)
        layout.addWidget(self.provider_detail_panel, stretch=1)
        return page

    def _build_ocr_model_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)

        # 左侧：在线 OCR 平台列表
        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        title_row = QHBoxLayout()
        list_title = QLabel("OCR 平台")
        list_title.setObjectName("settingsSectionHeading")
        list_count = QLabel(f"· {len(self._online_ocr_catalog)}")
        list_count.setObjectName("settingsCaption")
        title_row.addWidget(list_title)
        title_row.addWidget(list_count)
        title_row.addStretch(1)
        list_layout.addLayout(title_row)
        list_layout.addWidget(self.ocr_provider_list, stretch=1)

        # 右侧：连接配置卡片
        self.ocr_detail_panel = QWidget()
        detail_layout = QVBoxLayout(self.ocr_detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)

        config_card = QFrame()
        config_card.setObjectName("settingsCard")
        config_card_layout = QVBoxLayout(config_card)
        config_card_layout.setContentsMargins(14, 14, 14, 14)
        config_card_layout.setSpacing(10)
        config_heading = QLabel("连接配置")
        config_heading.setObjectName("settingsSectionHeading")
        config_card_layout.addWidget(config_heading)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.addRow("API Key", self._build_ocr_api_key_row())
        form.addRow("API 地址", self.ocr_base_url_input)
        form.addRow("模型", self.ocr_model_input)
        config_card_layout.addLayout(form)

        action_card = QFrame()
        action_card.setObjectName("settingsCard")
        action_card_layout = QVBoxLayout(action_card)
        action_card_layout.setContentsMargins(14, 14, 14, 14)
        action_card_layout.setSpacing(8)
        action_tools = QHBoxLayout()
        action_tools.addWidget(self.ocr_test_connection_button)
        action_tools.addStretch(1)
        action_card_layout.addLayout(action_tools)
        ocr_test_note = QLabel("检测会发起一次测试任务，可能消耗少量配额")
        ocr_test_note.setObjectName("statusLabel")
        ocr_test_note.setWordWrap(True)
        action_card_layout.addWidget(ocr_test_note)

        module_card = QFrame()
        module_card.setObjectName("settingsCard")
        module_card_layout = QVBoxLayout(module_card)
        module_card_layout.setContentsMargins(14, 14, 14, 14)
        module_card_layout.setSpacing(8)
        module_heading = QLabel("模块开关")
        module_heading.setObjectName("settingsSectionHeading")
        module_card_layout.addWidget(module_heading)
        module_card_layout.addWidget(self.ocr_online_table_checkbox)
        module_card_layout.addWidget(self.ocr_online_formula_checkbox)
        module_card_layout.addWidget(self.ocr_online_chart_checkbox)
        module_card_layout.addWidget(self.ocr_online_seal_checkbox)
        module_hint = QLabel("仅对支持的模型生效（表格/公式/印章→StructureV3；图表→StructureV3 与 VL）")
        module_hint.setObjectName("statusLabel")
        module_hint.setWordWrap(True)
        module_card_layout.addWidget(module_hint)

        detail_layout.addWidget(config_card)
        detail_layout.addWidget(action_card)
        detail_layout.addWidget(module_card)
        detail_layout.addStretch(1)

        # 右列：开关置于"连接配置"卡片上方、左对齐。开关须在 ocr_detail_panel 之外，
        # 否则被自身的 setEnabled(False) 一并禁用后无法再勾回。
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self.ocr_use_online_checkbox, alignment=Qt.AlignmentFlag.AlignLeft)
        right_layout.addWidget(self.ocr_detail_panel, stretch=1)

        body_layout.addWidget(list_panel)
        body_layout.addWidget(right_panel, stretch=1)
        page_layout.addWidget(body, stretch=1)
        return page

    def _build_api_key_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.api_key_input)
        layout.addWidget(self.toggle_api_key_button)
        return row

    def _build_ocr_api_key_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.ocr_api_key_input)
        layout.addWidget(self.ocr_toggle_api_key_button)
        return row

    def _build_template_page(self) -> QWidget:
        template_tab = QWidget()
        template_tab_layout = QVBoxLayout(template_tab)
        template_tab_layout.setContentsMargins(8, 8, 8, 8)
        template_tab_layout.setSpacing(10)
        template_shell = QWidget()
        template_shell_layout = QHBoxLayout(template_shell)
        template_shell_layout.setContentsMargins(0, 0, 0, 0)
        template_shell_layout.setSpacing(12)

        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        list_heading = QLabel("抽取模板")
        list_heading.setObjectName("settingsSectionHeading")
        list_layout.addWidget(list_heading)
        list_layout.addWidget(self.template_list, stretch=1)
        list_actions = QHBoxLayout()
        list_actions.addWidget(self.new_template_button)
        list_actions.addWidget(self.delete_template_button)
        list_actions.addWidget(self.reset_template_button)
        list_layout.addLayout(list_actions)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        editor_form = QFormLayout()
        editor_form.setHorizontalSpacing(12)
        editor_form.setVerticalSpacing(10)
        editor_form.addRow("名称", self.template_name_input)
        editor_form.addRow("Prompts", self.template_prompts_input)
        editor_form.addRow("Examples", self.template_examples_input)
        editor_layout.addLayout(editor_form)
        editor_tools = QHBoxLayout()
        editor_tools.addStretch(1)
        editor_tools.addWidget(self.format_button)
        editor_tools.addWidget(self.set_active_template_button)
        editor_layout.addLayout(editor_tools)

        template_shell_layout.addWidget(list_panel, stretch=2)
        template_shell_layout.addWidget(editor_panel, stretch=5)
        template_tab_layout.addWidget(template_shell, stretch=1)
        return template_tab

    def _build_business_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        ocr_heading = QLabel("OCR 设置")
        ocr_heading.setObjectName("settingsSectionHeading")
        layout.addWidget(ocr_heading)
        ocr_form = QFormLayout()
        ocr_form.addRow("排版模式", self.layout_parser_combo)
        layout.addLayout(ocr_form)
        layout.addWidget(self.layout_parser_online_hint)
        layout.addSpacing(layout.spacing())

        extract_heading = QLabel("抽取策略")
        extract_heading.setObjectName("settingsSectionHeading")
        layout.addWidget(extract_heading)
        grounding_form = QFormLayout()
        grounding_form.addRow("原文溯源", self.grounding_mode_combo)
        layout.addLayout(grounding_form)
        layout.addWidget(self.strategy_help_label)
        layout.addStretch(1)
        return page

    def _bind_events(self) -> None:
        self.settings_nav.currentRowChanged.connect(self.settings_stack.setCurrentIndex)
        self.provider_list.currentRowChanged.connect(self._on_provider_platform_row_changed)
        self.fetch_models_button.clicked.connect(self._on_fetch_models)
        self.provider_website_button.clicked.connect(lambda *_: self._open_provider_link(self.provider_website_button))
        self.provider_api_key_button.clicked.connect(lambda *_: self._open_provider_link(self.provider_api_key_button))
        self.provider_models_button.clicked.connect(lambda *_: self._open_provider_link(self.provider_models_button))
        self.model_input.currentTextChanged.connect(lambda *_: self._reset_test_connection_status())
        self.toggle_api_key_button.clicked.connect(self._on_toggle_api_key_visibility)
        self.ocr_toggle_api_key_button.clicked.connect(self._on_toggle_ocr_api_key_visibility)
        self.test_connection_button.clicked.connect(self._on_test_connection)
        self.set_active_provider_button.clicked.connect(self._on_set_active_provider)
        self.base_url_input.textChanged.connect(lambda *_: self._reset_test_connection_status())
        self.api_key_input.textChanged.connect(lambda *_: self._reset_test_connection_status())
        self.ocr_use_online_checkbox.toggled.connect(self._on_ocr_use_online_toggled)
        self.ocr_provider_list.currentRowChanged.connect(self._on_ocr_provider_row_changed)
        self.ocr_test_connection_button.clicked.connect(self._on_test_ocr_connection)
        self.allow_thinking_checkbox.toggled.connect(self.allow_thinking_hint.setVisible)
        self.layout_parser_combo.currentIndexChanged.connect(self._on_layout_parser_changed)
        self.template_list.currentRowChanged.connect(self._on_template_selection_changed)
        self.new_template_button.clicked.connect(self._on_new_template)
        self.delete_template_button.clicked.connect(self._on_delete_template)
        self.reset_template_button.clicked.connect(self._on_reset_template)
        self.set_active_template_button.clicked.connect(self._on_set_active_template)
        self.format_button.clicked.connect(self._on_format_examples)
        self.save_button.clicked.connect(self._on_save)
        self.cancel_button.clicked.connect(self.reject)
        self.template_name_input.textChanged.connect(self._on_template_editor_changed)
        self.template_prompts_input.textChanged.connect(self._on_template_editor_changed)
        self.template_examples_input.textChanged.connect(self._on_template_editor_changed)

    def _apply_style(self) -> None:
        self.setStyleSheet(generate_settings_qss())

    def _load_from_controller(self) -> None:
        config = project_active_template_config(self.controller.load_config())
        self._loaded_config = config
        self.current_provider_platform_id = getattr(config, "provider_platform_id", "custom") or "custom"
        self.editing_provider_platform_id = self.current_provider_platform_id
        self._provider_profile_drafts = self._profile_drafts_from_config(config)
        self._loading_provider_profile = True
        try:
            self._refresh_provider_list()
            self._select_provider_platform(self.editing_provider_platform_id)
        finally:
            self._loading_provider_profile = False
        self._update_set_active_provider_button_state()
        self.ollama_num_ctx_input.setValue(self._sanitize_ollama_num_ctx(config.ollama_num_ctx))
        self.allow_thinking_checkbox.setChecked(bool(getattr(config, "allow_thinking", False)))
        self.allow_thinking_hint.setVisible(self.allow_thinking_checkbox.isChecked())
        grounding_mode_index = self.grounding_mode_combo.findData(config.grounding_mode)
        if grounding_mode_index >= 0:
            self.grounding_mode_combo.setCurrentIndex(grounding_mode_index)
        self._load_online_ocr_from_config(config)
        self._set_layout_parser_selection(config.ocr_layout_parser)
        self._layout_parser_user_edited = False
        self._set_api_key_visibility(False)
        self._set_ocr_api_key_visibility(False)
        self._catalog = TemplateCatalog.load(config.templates, config.active_template_id)
        self._pending_active_template_id = self._catalog.active_id
        self._seed_template_drafts_from_catalog()
        self._refresh_template_list(select_id=self._catalog.active_id)
        self._update_set_active_button_state()
        self._set_validation_error("")
        self._update_bottom_bar_current_use()
        self._restore_bottom_bar_status()

    def _set_validation_error(self, message: str) -> None:
        self.validation_error_label.setText(message)
        self.validation_error_label.setVisible(bool(message))

    def _update_bottom_bar_current_use(self) -> None:
        config = self._loaded_config
        if config is None:
            return
        entry = get_provider_entry(self.current_provider_platform_id)
        self.bottom_bar_provider_name_label.setText(entry.display_name)
        icon = self._provider_icon(entry.logo_asset)
        self.bottom_bar_provider_icon_label.setPixmap(icon.pixmap(16, 16))
        draft = self._provider_profile_drafts.get(self.current_provider_platform_id, {})
        self.bottom_bar_model_label.setText(draft.get("model", "") or config.model)
        self.bottom_bar_template_label.setText(
            self._catalog.template_by_id(self._pending_active_template_id).name
        )

    def _restore_bottom_bar_status(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._bottom_bar_status_generation:
            return
        try:
            self.bottom_bar_status_label.setText("已载入配置 · config.json")
            self.bottom_bar_status_label.setStyleSheet("")
        except RuntimeError:
            # 对话框已关闭、底层 C++ 对象被回收 - 悬挂回调，静默忽略
            return

    def _show_bottom_bar_status(self, message: str, color: str, *, restore: bool = True) -> None:
        self._bottom_bar_status_generation += 1
        generation = self._bottom_bar_status_generation
        self.bottom_bar_status_label.setText(message)
        self.bottom_bar_status_label.setStyleSheet(f"color: {color};")
        if restore:
            QTimer.singleShot(2000, lambda: self._restore_bottom_bar_status(generation))

    def _set_api_key_visibility(self, visible: bool) -> None:
        self._api_key_visible = visible
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.api_key_input.setEchoMode(mode)
        icon_name = "yanjing" if visible else "yanjing_yincang_o"
        self.toggle_api_key_button.setIcon(load_icon(icon_name))

    def _set_ocr_api_key_visibility(self, visible: bool) -> None:
        self._ocr_api_key_visible = visible
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.ocr_api_key_input.setEchoMode(mode)
        icon_name = "yanjing" if visible else "yanjing_yincang_o"
        self.ocr_toggle_api_key_button.setIcon(load_icon(icon_name))

    @staticmethod
    def _default_provider_profiles() -> dict[str, dict[str, str]]:
        return profiles_as_dict(catalog_default_profiles())

    def _profile_drafts_from_config(self, config: AppConfig) -> dict[str, dict[str, str]]:
        drafts = self._default_provider_profiles()
        raw_profiles = getattr(config, "provider_profiles", {})
        if isinstance(raw_profiles, dict):
            for platform_id in PROVIDER_PLATFORM_IDS:
                raw_profile = raw_profiles.get(platform_id)
                if isinstance(raw_profile, dict):
                    drafts[platform_id] = {
                        "base_url": str(raw_profile.get("base_url") or drafts[platform_id]["base_url"]),
                        "api_key": str(raw_profile.get("api_key") or ""),
                        "model": str(raw_profile.get("model") or drafts[platform_id]["model"]),
                    }
        current_platform = getattr(config, "provider_platform_id", "custom") or "custom"
        drafts[current_platform] = {
            "base_url": config.base_url or drafts[current_platform]["base_url"],
            "api_key": config.api_key or "",
            "model": config.model or drafts[current_platform]["model"],
        }
        return drafts

    def _build_current_use_row(self, label: str, *, active: bool, logo_asset: str = "") -> QWidget:
        row = QWidget()
        row.setObjectName("currentUseRow")
        row.setProperty("active", active)
        row.setProperty("selected", False)
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setFixedHeight(40)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)
        if logo_asset:
            logo = QLabel()
            logo.setFixedSize(20, 20)
            logo.setPixmap(self._provider_icon(logo_asset).pixmap(20, 20))
            layout.addWidget(logo)
        layout.addWidget(QLabel(label))
        layout.addStretch(1)
        badge = QLabel("当前使用")
        badge.setObjectName("currentUseBadge")
        badge.setVisible(active)
        layout.addWidget(badge)
        return row

    @staticmethod
    def _refresh_list_row_selection(list_widget: QListWidget) -> None:
        current_row = list_widget.currentRow()
        for index in range(list_widget.count()):
            item = list_widget.item(index)
            if item is None:
                continue
            row_widget = list_widget.itemWidget(item)
            if row_widget is None:
                continue
            row_widget.setProperty("selected", index == current_row)
            row_widget.style().unpolish(row_widget)
            row_widget.style().polish(row_widget)
            row_widget.update()

    def _refresh_provider_list(self) -> None:
        self.provider_list.clear()
        for entry in self._provider_catalog:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 40))
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.provider_list.addItem(item)
            self.provider_list.setItemWidget(
                item,
                self._build_current_use_row(
                    entry.display_name,
                    active=entry.id == self.current_provider_platform_id,
                    logo_asset=entry.logo_asset,
                ),
            )
        self._select_provider_platform(self.editing_provider_platform_id)

    def _select_provider_platform(self, platform_id: str) -> None:
        for index in range(self.provider_list.count()):
            item = self.provider_list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == platform_id:
                self.provider_list.setCurrentRow(index)
                return

    def _on_provider_platform_row_changed(self, row: int) -> None:
        self._refresh_list_row_selection(self.provider_list)
        if row < 0:
            return
        item = self.provider_list.item(row)
        if item is None:
            return
        platform_id = str(item.data(Qt.ItemDataRole.UserRole))
        if self._loading_provider_profile:
            self.editing_provider_platform_id = platform_id
            self._load_provider_draft_into_inputs(platform_id)
            self._update_set_active_provider_button_state()
            return
        self._save_current_provider_draft()
        self.editing_provider_platform_id = platform_id
        self._load_provider_draft_into_inputs(platform_id)
        self._update_set_active_provider_button_state()
        self._set_validation_error("")
        self._reset_test_connection_status()
        self.provider_fetch_error_label.setText("")
        self.provider_fetch_error_label.setVisible(False)
        self.fetch_models_button.setEnabled(True)
        self.fetch_models_button.setText("获取模型列表")

    def _save_current_provider_draft(self) -> None:
        self._provider_profile_drafts[self.editing_provider_platform_id] = {
            "base_url": self.base_url_input.text().strip(),
            "api_key": self._resolve_api_key(),
            "model": self.model_input.currentText().strip(),
        }

    def _load_provider_draft_into_inputs(self, platform_id: str) -> None:
        entry = get_provider_entry(platform_id)
        profile = self._provider_profile_drafts.get(platform_id, {})
        base_url = profile.get("base_url", "") or entry.default_base_url
        model = profile.get("model", "") or (entry.recommended_models[0] if entry.recommended_models else "")
        self.provider_name_label.setText(entry.display_name)
        hostname = urlparse(entry.website_url).hostname or ""
        self.provider_subtitle_label.setText(hostname.removeprefix("www."))
        self.provider_subtitle_label.setVisible(bool(hostname))
        self._set_provider_logo(entry.logo_asset)
        self._set_provider_link_button(self.provider_website_button, entry.website_url)
        self._set_provider_link_button(self.provider_api_key_button, entry.api_key_url)
        self._set_provider_link_button(self.provider_models_button, entry.models_url)
        self.base_url_input.setText(base_url)
        self.api_key_input.setText(profile.get("api_key", ""))
        self._set_model_options(entry.recommended_models, self._remote_models_by_platform.get(platform_id, ()), model)
        visible = entry.runtime_provider == "ollama"
        self.ollama_num_ctx_label.setVisible(visible)
        self.ollama_num_ctx_input.setVisible(visible)
        self.api_key_input.setPlaceholderText("Ollama 可选" if visible else "此服务商必填")

    def _set_model_options(self, recommended: tuple[str, ...], remote: tuple[str, ...], current: str) -> None:
        self.model_input.blockSignals(True)
        self.model_input.clear()
        seen: set[str] = set()
        for model in (*recommended, *remote):
            if model and model not in seen:
                self.model_input.addItem(model)
                seen.add(model)
        self.model_input.setEditText(current)
        self.model_input.blockSignals(False)

    def _provider_icon(self, logo_asset: str) -> QIcon:
        if not logo_asset:
            return QIcon()
        path = _assets_dir() / logo_asset
        return QIcon(str(path)) if path.exists() else QIcon()

    def _external_link_icon(self) -> QIcon:
        path = _assets_dir() / "icons" / "external-link.svg"
        return QIcon(str(path)) if path.exists() else QIcon()

    def _set_provider_logo(self, logo_asset: str) -> None:
        icon = self._provider_icon(logo_asset)
        if icon.isNull():
            self.provider_logo_label.clear()
            return
        pixmap = icon.pixmap(32, 32)
        self.provider_logo_label.setPixmap(pixmap)

    def _set_provider_link_button(self, button: QToolButton, url: str) -> None:
        button.setProperty("url", url)
        button.setVisible(bool(url))
        button.setEnabled(bool(url))

    def _open_provider_link(self, button: QToolButton) -> None:
        url = str(button.property("url") or "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_fetch_models(self) -> None:
        self._save_current_provider_draft()
        entry = get_provider_entry(self.editing_provider_platform_id)
        self.provider_fetch_error_label.setText("")
        self.provider_fetch_error_label.setVisible(False)
        self._fetching_models_platform_id = entry.id
        self.fetch_models_button.setEnabled(False)
        self.fetch_models_button.setText("获取中...")
        runnable = _ModelFetchRunnable(
            self.controller,
            platform_id=entry.id,
            entry=entry,
            base_url=self.base_url_input.text().strip(),
            api_key=self._resolve_api_key(),
        )
        self._active_model_fetches.add(runnable)
        runnable.signals.finished.connect(
            lambda platform_id, result: self._finish_fetch_models(runnable, platform_id, result),
            Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(runnable)

    def _finish_fetch_models(self, runnable: _ModelFetchRunnable, platform_id: str, result: ModelFetchResult) -> None:
        self._active_model_fetches.discard(runnable)
        self._on_fetch_models_done(platform_id, result)

    def _on_fetch_models_done(self, platform_id: str, result: ModelFetchResult) -> None:
        try:
            if self._fetching_models_platform_id == platform_id:
                self._fetching_models_platform_id = None
                self.fetch_models_button.setEnabled(True)
                self.fetch_models_button.setText("获取模型列表")
            if platform_id != self.editing_provider_platform_id:
                return
            if not result.ok:
                self.provider_fetch_error_label.setText(result.error)
                self.provider_fetch_error_label.setVisible(True)
                return
            entry = get_provider_entry(platform_id)
            self._remote_models_by_platform[entry.id] = result.models
            self.provider_fetch_error_label.setText("")
            self.provider_fetch_error_label.setVisible(False)
            current = self.model_input.currentText().strip() or (result.models[0] if result.models else "")
            self._set_model_options(entry.recommended_models, result.models, current)
        except RuntimeError:
            return

    def _on_toggle_api_key_visibility(self) -> None:
        self._set_api_key_visibility(not self._api_key_visible)

    def _on_toggle_ocr_api_key_visibility(self) -> None:
        self._set_ocr_api_key_visibility(not self._ocr_api_key_visible)

    def _on_spinner_tick(self) -> None:
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        self.test_connection_button.setText(f"{frame} 检测中…")
        self._spinner_index += 1

    def _reset_test_connection_status(self) -> None:
        self._connection_check_generation += 1
        self._spinner_timer.stop()
        self.test_connection_button.setEnabled(True)
        self.test_connection_button.setText("✓ 检测连接")
        self.test_connection_button.setToolTip("")
        self.test_connection_button.setStyleSheet("")

    def _on_test_connection(self) -> None:
        self._connection_check_generation += 1
        generation = self._connection_check_generation
        entry = get_provider_entry(self.editing_provider_platform_id)
        api_key = self._resolve_api_key()
        if entry.requires_api_key and not api_key.strip():
            self._on_test_connection_done(
                ConnectionCheckResult(ok=False, detail="请先填写 API Key 后再检测连接。"),
                generation=generation,
            )
            return
        self.test_connection_button.setEnabled(False)
        self.test_connection_button.setToolTip("")
        self.test_connection_button.setStyleSheet("")
        self._spinner_index = 0
        self._on_spinner_tick()
        self._spinner_timer.start()
        provider = runtime_provider_for_platform(entry.id)
        base_url = self.base_url_input.text()
        model = self.model_input.currentText().strip()
        runnable = _ConnectionCheckRunnable(
            check_callable=lambda: self.controller.test_connection(
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
            )
        )
        self._active_connection_checks.add(runnable)
        runnable.signals.finished.connect(
            lambda result: self._finish_test_connection(runnable, result, generation=generation),
            Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(runnable)

    def _finish_test_connection(
        self, runnable: _ConnectionCheckRunnable, result: ConnectionCheckResult, *, generation: int
    ) -> None:
        self._active_connection_checks.discard(runnable)
        self._on_test_connection_done(result, generation=generation)

    def _on_test_connection_done(
        self, result: "ConnectionCheckResult", *, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self._connection_check_generation:
            return
        try:
            self._spinner_timer.stop()
            self.test_connection_button.setEnabled(True)
            if result.ok and result.model_warning:
                self.test_connection_button.setText("⚠ 模型名待确认")
                self.test_connection_button.setStyleSheet(f"color: {COLORS['semantic_warning']};")
                self.test_connection_button.setToolTip(result.model_warning)
            elif result.ok:
                self.test_connection_button.setText("✓ 连接成功")
                self.test_connection_button.setStyleSheet(f"color: {COLORS['success']};")
                self.test_connection_button.setToolTip(result.detail)
            else:
                self.test_connection_button.setText("✗ 连接失败")
                self.test_connection_button.setStyleSheet(f"color: {COLORS['danger']};")
                self.test_connection_button.setToolTip(result.detail)
        except RuntimeError:
            # 对话框已关闭、底层 C++ 对象被回收 — 悬挂回调，静默忽略
            return

    def _online_ocr_profile_drafts_from_config(self, config: AppConfig) -> dict[str, dict[str, str]]:
        drafts = catalog_default_online_profiles()
        raw_profiles = getattr(config, "ocr_online_profiles", {})
        if isinstance(raw_profiles, dict):
            for entry in self._online_ocr_catalog:
                raw_profile = raw_profiles.get(entry.id)
                if isinstance(raw_profile, dict):
                    drafts[entry.id] = {
                        "base_url": str(raw_profile.get("base_url") or drafts[entry.id]["base_url"]),
                        "api_key": str(raw_profile.get("api_key") or ""),
                        "model": str(raw_profile.get("model") or drafts[entry.id]["model"]),
                    }
        return drafts

    def _load_online_ocr_from_config(self, config: AppConfig) -> None:
        self._online_ocr_profile_drafts = self._online_ocr_profile_drafts_from_config(config)
        platform_id = getattr(config, "ocr_online_platform_id", DEFAULT_ONLINE_OCR_PLATFORM_ID) or DEFAULT_ONLINE_OCR_PLATFORM_ID
        if platform_id not in self._online_ocr_profile_drafts:
            platform_id = DEFAULT_ONLINE_OCR_PLATFORM_ID
        self.editing_online_ocr_platform_id = platform_id
        self._loading_online_ocr_profile = True
        try:
            self._refresh_online_ocr_list()
            self._select_online_ocr_platform(platform_id)
        finally:
            self._loading_online_ocr_profile = False
        use_online = bool(getattr(config, "ocr_use_online", False))
        self.ocr_online_table_checkbox.setChecked(
            bool(getattr(config, "ocr_online_use_table_recognition", False))
        )
        self.ocr_online_formula_checkbox.setChecked(
            bool(getattr(config, "ocr_online_use_formula_recognition", False))
        )
        self.ocr_online_chart_checkbox.setChecked(
            bool(getattr(config, "ocr_online_use_chart_recognition", False))
        )
        self.ocr_online_seal_checkbox.setChecked(
            bool(getattr(config, "ocr_online_use_seal_recognition", False))
        )
        self.ocr_use_online_checkbox.setChecked(use_online)
        self.ocr_detail_panel.setEnabled(use_online)
        self._apply_layout_parser_online_state(use_online)

    def _refresh_online_ocr_list(self) -> None:
        self.ocr_provider_list.clear()
        for entry in self._online_ocr_catalog:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 40))
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.ocr_provider_list.addItem(item)
            self.ocr_provider_list.setItemWidget(
                item,
                self._build_current_use_row(entry.display_name, active=False),
            )

    def _select_online_ocr_platform(self, platform_id: str) -> None:
        for index in range(self.ocr_provider_list.count()):
            item = self.ocr_provider_list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == platform_id:
                self.ocr_provider_list.setCurrentRow(index)
                return

    def _on_ocr_provider_row_changed(self, row: int) -> None:
        self._refresh_list_row_selection(self.ocr_provider_list)
        if row < 0:
            return
        item = self.ocr_provider_list.item(row)
        if item is None:
            return
        platform_id = str(item.data(Qt.ItemDataRole.UserRole))
        if not self._loading_online_ocr_profile:
            self._save_current_online_ocr_draft()
        self.editing_online_ocr_platform_id = platform_id
        self._load_online_ocr_draft_into_inputs(platform_id)
        if not self._loading_online_ocr_profile:
            self._reset_ocr_test_connection_status()

    def _save_current_online_ocr_draft(self) -> None:
        self._online_ocr_profile_drafts[self.editing_online_ocr_platform_id] = {
            "base_url": self.ocr_base_url_input.text().strip(),
            "api_key": self.ocr_api_key_input.text().strip(),
            "model": self.ocr_model_input.currentText().strip(),
        }

    def _load_online_ocr_draft_into_inputs(self, platform_id: str) -> None:
        entry = get_online_ocr_entry(platform_id)
        profile = self._online_ocr_profile_drafts.get(platform_id, {})
        base_url = profile.get("base_url", "") or entry.default_base_url
        model = profile.get("model", "") or (entry.recommended_models[0] if entry.recommended_models else "")
        self.ocr_base_url_input.setText(base_url)
        self.ocr_api_key_input.setText(profile.get("api_key", ""))
        self._set_ocr_model_options(entry.recommended_models, model)

    def _set_ocr_model_options(self, recommended: tuple[str, ...], current: str) -> None:
        self.ocr_model_input.blockSignals(True)
        self.ocr_model_input.clear()
        seen: set[str] = set()
        for model in recommended:
            if model and model not in seen:
                self.ocr_model_input.addItem(model)
                seen.add(model)
        self.ocr_model_input.setEditText(current)
        self.ocr_model_input.blockSignals(False)

    def _on_ocr_use_online_toggled(self, checked: bool) -> None:
        self.ocr_detail_panel.setEnabled(checked)
        self._apply_layout_parser_online_state(checked)

    def _apply_layout_parser_online_state(self, online: bool) -> None:
        """在线 OCR 启用时禁用排版模式下拉并提示其不生效（仅本地 Paddle 路径消费排版设置）。"""
        self.layout_parser_combo.setEnabled(not online)
        self.layout_parser_online_hint.setVisible(online)

    def _reset_ocr_test_connection_status(self) -> None:
        self._ocr_connection_check_generation += 1
        self.ocr_test_connection_button.setEnabled(True)
        self.ocr_test_connection_button.setText("✓ 检测连接")
        self.ocr_test_connection_button.setToolTip("")
        self.ocr_test_connection_button.setStyleSheet("")

    def _on_test_ocr_connection(self) -> None:
        self._ocr_connection_check_generation += 1
        generation = self._ocr_connection_check_generation
        base_url = self.ocr_base_url_input.text().strip()
        api_key = self.ocr_api_key_input.text().strip()
        model = self.ocr_model_input.currentText().strip()
        if not api_key:
            self._on_test_ocr_connection_done(
                ConnectionCheckResult(ok=False, detail="请先填写 API Key 后再检测连接。"),
                generation=generation,
            )
            return
        self.ocr_test_connection_button.setEnabled(False)
        self.ocr_test_connection_button.setText("检测中…")
        self.ocr_test_connection_button.setToolTip("")
        self.ocr_test_connection_button.setStyleSheet("")
        runnable = _ConnectionCheckRunnable(
            check_callable=lambda: self.controller.test_ocr_connection(
                base_url=base_url,
                api_key=api_key,
                model=model,
            )
        )
        self._active_ocr_connection_checks.add(runnable)
        runnable.signals.finished.connect(
            lambda result: self._finish_test_ocr_connection(runnable, result, generation=generation),
            Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(runnable)

    def _finish_test_ocr_connection(
        self, runnable: _ConnectionCheckRunnable, result: ConnectionCheckResult, *, generation: int
    ) -> None:
        self._active_ocr_connection_checks.discard(runnable)
        self._on_test_ocr_connection_done(result, generation=generation)

    def _on_test_ocr_connection_done(
        self, result: "ConnectionCheckResult", *, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self._ocr_connection_check_generation:
            return
        try:
            self.ocr_test_connection_button.setEnabled(True)
            if result.ok:
                self.ocr_test_connection_button.setText("✓ 连接成功")
                self.ocr_test_connection_button.setStyleSheet(f"color: {COLORS['success']};")
                self.ocr_test_connection_button.setToolTip(result.detail)
            else:
                self.ocr_test_connection_button.setText("✗ 连接失败")
                self.ocr_test_connection_button.setStyleSheet(f"color: {COLORS['danger']};")
                self.ocr_test_connection_button.setToolTip(result.detail)
        except RuntimeError:
            # 对话框已关闭、底层 C++ 对象被回收 — 悬挂回调，静默忽略
            return

    def _on_layout_parser_changed(self) -> None:
        self._layout_parser_user_edited = True

    def _refresh_template_list(self, *, select_id: str | None) -> None:
        self.template_list.clear()
        for entry in self._catalog.list_entries():
            draft = self._template_drafts.get(entry.id, {})
            name = str(draft.get("name", entry.name)).strip() or "<未命名>"
            label = f"{name}（内置）" if entry.builtin else name
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 40))
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            self.template_list.addItem(item)
            self.template_list.setItemWidget(
                item,
                self._build_current_use_row(
                    label,
                    active=entry.id == self._pending_active_template_id,
                ),
            )
        self._select_template_item(select_id or self._catalog.active_id)

    def _update_set_active_button_state(self) -> None:
        selected = self._current_template_id
        if selected and selected == self._pending_active_template_id:
            self.set_active_template_button.setEnabled(False)
            self.set_active_template_button.setText("已是当前模板")
        else:
            self.set_active_template_button.setEnabled(bool(selected))
            self.set_active_template_button.setText("设为当前模板")

    def _update_set_active_provider_button_state(self) -> None:
        if self.editing_provider_platform_id == self.current_provider_platform_id:
            self.set_active_provider_button.setEnabled(False)
            self.set_active_provider_button.setText("已是当前模型")
        else:
            self.set_active_provider_button.setEnabled(True)
            self.set_active_provider_button.setText("设为当前模型")

    def _seed_template_drafts_from_catalog(self) -> None:
        self._template_drafts = {}
        for entry in self._catalog.list_entries():
            template = self._catalog.template_by_id(entry.id)
            self._template_drafts[entry.id] = {
                "builtin": entry.builtin,
                "name": template.name,
                "prompts": template.description,
                "examples_text": json.dumps(template.examples, ensure_ascii=False, indent=2),
            }

    def _select_template_item(self, template_id: str) -> None:
        for index in range(self.template_list.count()):
            item = self.template_list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == template_id:
                self.template_list.setCurrentRow(index)
                return
        if self.template_list.count():
            self.template_list.setCurrentRow(0)

    def _on_template_selection_changed(self, row: int) -> None:
        self._refresh_list_row_selection(self.template_list)
        if row < 0:
            return
        item = self.template_list.item(row)
        if item is None:
            return
        template_id = str(item.data(Qt.ItemDataRole.UserRole))
        self._current_template_id = template_id
        draft = self._template_drafts.get(template_id, {})
        self._syncing_template_fields = True
        self.template_name_input.setText(str(draft.get("name", "")))
        self.template_prompts_input.setPlainText(str(draft.get("prompts", "")))
        self.template_examples_input.setPlainText(str(draft.get("examples_text", "")))
        self._syncing_template_fields = False
        is_builtin = bool(draft.get("builtin", False))
        self.delete_template_button.setEnabled(not is_builtin)
        self.reset_template_button.setEnabled(is_builtin)
        self._update_set_active_button_state()
        self._set_validation_error("")

    def _on_new_template(self) -> None:
        index = 1
        while True:
            candidate = "新模板" if index == 1 else f"新模板 {index}"
            result = self._catalog.validate_draft(
                name=candidate,
                prompts="请描述抽取规则",
                examples='[["字段1","字段2"],["示例1","示例2"]]',
            )
            if result.ok:
                template_id = self._catalog.create_user_template(
                    candidate,
                    "请描述抽取规则",
                    '[["字段1","字段2"],["示例1","示例2"]]',
                )
                break
            index += 1
        self._template_drafts[template_id] = {
            "builtin": False,
            "name": candidate,
            "prompts": "请描述抽取规则",
            "examples_text": '[\n  ["字段1", "字段2"],\n  ["示例1", "示例2"]\n]',
        }
        self._refresh_template_list(select_id=template_id)

    def _on_delete_template(self) -> None:
        if not self._current_template_id:
            return
        deleted_id = self._current_template_id
        self._catalog.delete(deleted_id)
        self._template_drafts.pop(deleted_id, None)
        if self._pending_active_template_id == deleted_id:
            self._pending_active_template_id = BUILTIN_INVOICE_ID
        self._refresh_template_list(select_id=self._catalog.active_id)

    def _on_reset_template(self) -> None:
        if not self._current_template_id:
            return
        self._catalog.reset(self._current_template_id)
        template = self._catalog.template_by_id(self._current_template_id)
        self._template_drafts[self._current_template_id] = {
            "builtin": True,
            "name": template.name,
            "prompts": template.description,
            "examples_text": json.dumps(template.examples, ensure_ascii=False, indent=2),
        }
        self._refresh_template_list(select_id=self._current_template_id)

    def _on_set_active_template(self) -> None:
        if not self._current_template_id:
            return
        if self._current_template_id == self._pending_active_template_id:
            return
        self._pending_active_template_id = self._current_template_id
        self._refresh_template_list(select_id=self._current_template_id)
        self._update_set_active_button_state()
        self._update_bottom_bar_current_use()

    def _on_set_active_provider(self) -> None:
        if self.editing_provider_platform_id == self.current_provider_platform_id:
            return
        self.current_provider_platform_id = self.editing_provider_platform_id
        self._refresh_provider_list()
        self._update_set_active_provider_button_state()
        self._update_bottom_bar_current_use()

    def _on_template_editor_changed(self) -> None:
        if self._syncing_template_fields or not self._current_template_id:
            return
        draft = self._template_drafts.setdefault(self._current_template_id, {})
        draft["name"] = self.template_name_input.text()
        draft["prompts"] = self.template_prompts_input.toPlainText()
        draft["examples_text"] = self.template_examples_input.toPlainText()
        self._refresh_template_list(select_id=self._current_template_id)
        self._set_validation_error("")

    def _on_format_examples(self) -> None:
        raw = self.template_examples_input.toPlainText()
        try:
            formatted = self.controller.format_examples(raw)
        except Exception as exc:
            self._set_validation_error(f"examples_raw: {exc}")
            self._show_bottom_bar_status("格式化失败", COLORS["danger"])
            return
        self.template_examples_input.setPlainText(json.dumps(formatted, ensure_ascii=False, indent=2))
        self._set_validation_error("")
        self._show_bottom_bar_status("示例已格式化", COLORS["success"])

    def _on_save(self) -> None:
        valid, config_or_error = self._build_validated_config()
        if not valid:
            error_message = str(config_or_error)
            self._set_validation_error(error_message)
            self._show_bottom_bar_status("保存被阻止", COLORS["danger"])
            return
        config = config_or_error
        try:
            self.controller.save_config(config)
            self._loaded_config = config
            self.current_provider_platform_id = (
                getattr(config, "provider_platform_id", "custom") or "custom"
            )
            self._catalog = TemplateCatalog.load(config.templates, config.active_template_id)
            self._pending_active_template_id = self._catalog.active_id
            self._update_bottom_bar_current_use()
            self._set_validation_error("")
            self._show_bottom_bar_status("已保存", COLORS["success"], restore=False)
            self.accept()
        except Exception as exc:
            self._set_validation_error(str(exc))
            QMessageBox.warning(self, "保存失败", str(exc))
            self._show_bottom_bar_status("保存失败", COLORS["danger"])

    def _build_validated_config(self) -> tuple[bool, AppConfig | str]:
        base_config = self._loaded_config or self.controller.load_config()
        serialized = self._catalog.serialize()
        working_catalog = TemplateCatalog.load(serialized["templates"], serialized["active_template_id"])
        drafts: dict[str, tuple[str, str, str]] = {}
        for entry in working_catalog.list_entries():
            draft = self._template_drafts.get(entry.id)
            if draft is None:
                continue
            drafts[entry.id] = (
                str(draft.get("name", "")),
                str(draft.get("prompts", "")),
                str(draft.get("examples_text", "")),
            )
        try:
            working_catalog.apply_updates(drafts)
        except ExtractServiceError as exc:
            return False, exc.message
        working_catalog.active_id = self._pending_active_template_id
        serialized = working_catalog.serialize()
        projected = project_active_template_config(
            replace(
                base_config,
                templates=list(serialized["templates"]),  # type: ignore[arg-type]
                active_template_id=str(serialized["active_template_id"]),
                ocr_use_doc_unwarping=False,
            )
        )
        self._save_current_provider_draft()
        provider_profiles = {
            platform_id: dict(profile)
            for platform_id, profile in self._provider_profile_drafts.items()
            if platform_id in PROVIDER_PLATFORM_IDS
        }
        runtime_provider = runtime_provider_for_platform(self.editing_provider_platform_id)
        self._save_current_online_ocr_draft()
        online_ocr_profiles = {
            platform_id: dict(profile)
            for platform_id, profile in self._online_ocr_profile_drafts.items()
        }
        form_data = SettingsFormData(
            provider=runtime_provider,
            provider_platform_id=self.editing_provider_platform_id,
            base_url=self.base_url_input.text().strip(),
            api_key=self._resolve_api_key(),
            model=self.model_input.currentText().strip(),
            prompts=projected.prompts,
            raw_examples=projected.examples_raw,
            grounding_mode=str(self.grounding_mode_combo.currentData()),
            ocr_layout_parser=str(self.layout_parser_combo.currentData()),
            ocr_layout_parser_user_edited=self._layout_parser_user_edited,
            ocr_profile=base_config.ocr_profile,
            extraction_profile=base_config.extraction_profile,
            ollama_num_ctx=self.ollama_num_ctx_input.value(),
            allow_thinking=self.allow_thinking_checkbox.isChecked(),
            provider_profiles=provider_profiles,
            ocr_use_online=self.ocr_use_online_checkbox.isChecked(),
            ocr_online_platform_id=self.editing_online_ocr_platform_id,
            ocr_online_profiles=online_ocr_profiles,
            ocr_online_use_table_recognition=self.ocr_online_table_checkbox.isChecked(),
            ocr_online_use_formula_recognition=self.ocr_online_formula_checkbox.isChecked(),
            ocr_online_use_chart_recognition=self.ocr_online_chart_checkbox.isChecked(),
            ocr_online_use_seal_recognition=self.ocr_online_seal_checkbox.isChecked(),
        )
        return build_validated_config(
            form_data,
            format_examples=self.controller.format_examples,
            base_config=projected,
        )

    def _resolve_api_key(self) -> str:
        return self.api_key_input.text().strip()

    def _set_layout_parser_selection(self, parser: str) -> None:
        selected = parser if parser in {"single_line", "multi_para", "none"} else "multi_para"
        index = self.layout_parser_combo.findData(selected)
        if index >= 0:
            self.layout_parser_combo.setCurrentIndex(index)
        if parser in {"single_line", "multi_para", "none"}:
            self.layout_parser_combo.setToolTip("")
        else:
            self.layout_parser_combo.setToolTip(f"当前配置使用 {parser}；只有主动切换后才会改写为界面选项值")

    @staticmethod
    def _sanitize_ollama_num_ctx(value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8192
        return parsed if 2048 <= parsed <= 32768 else 8192
