from __future__ import annotations

from dataclasses import replace
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from shiboken6 import delete

from src.extract.connection_check import ConnectionCheckResult
from src.extract.model_fetcher import ModelFetchResult
from src.io.config_store import ConfigStore
from src.ui.settings_dialog import SettingsDialog


class _Controller:
    def __init__(self) -> None:
        self.config = ConfigStore.default_config()

    def load_config(self):
        return self.config

    def save_config(self, config):
        self.config = config

    def format_examples(self, raw_examples: str):
        from src.extract.example_parser import format_examples

        return format_examples(raw_examples)


def test_settings_dialog_maps_allow_thinking_checkbox_to_config():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())
    dialog.base_url_input.setText("http://localhost")
    dialog.api_key_input.setText("key")
    dialog.model_input.setEditText("model")

    dialog.allow_thinking_checkbox.setChecked(True)
    ok, result = dialog._build_validated_config()

    assert ok is True
    assert not isinstance(result, str)
    assert result.allow_thinking is True


def test_settings_dialog_applies_notion_theme_stylesheet():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    style = dialog.styleSheet()

    assert "Geist" in style
    assert "#5B3DE3" in style
    assert "#C9342F" in style
    assert "QFrame#settingsCard" in style


def test_settings_dialog_left_navigation_has_dedicated_style_hook():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    assert dialog.settings_nav.objectName() == "settingsNav"
    assert dialog.settings_nav.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_settings_dialog_nav_renames_model_service_and_adds_ocr_entry():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    nav_labels = [dialog.settings_nav.item(row).text() for row in range(dialog.settings_nav.count())]

    assert nav_labels[:2] == ["抽取模型", "OCR模型"]
    assert "模型服务" not in nav_labels
    assert nav_labels == ["抽取模型", "OCR模型", "抽取模板", "业务设置"]


def test_settings_dialog_ocr_online_toggle_copy_mentions_mobile_model():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    assert dialog.ocr_use_online_checkbox.text() == "启用在线OCR（离线已内置PaddleOCR Mobile模型）"


def test_settings_dialog_template_editor_action_buttons_align_to_examples_right_edge():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())
    dialog.settings_nav.setCurrentRow(2)
    dialog.resize(960, 640)
    dialog.show()
    app.processEvents()

    examples_right = dialog.template_examples_input.mapTo(dialog, dialog.template_examples_input.rect().topRight()).x()
    set_active_right = dialog.set_active_template_button.mapTo(dialog, dialog.set_active_template_button.rect().topRight()).x()
    format_right = dialog.format_button.mapTo(dialog, dialog.format_button.rect().topRight()).x()

    assert abs(set_active_right - examples_right) <= 1
    assert format_right < set_active_right


def test_settings_dialog_shows_bottom_bar_actions_and_localized_copy():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    assert dialog.size().width() == 960
    assert dialog.size().height() == 640
    assert dialog.windowTitle() == "设置"
    assert dialog.settings_bottom_bar.objectName() == "settingsBottomBar"
    assert dialog.bottom_bar_status_label.text() == "已载入配置 · config.json"
    assert dialog.cancel_button.text() == "取消"
    assert dialog.save_button.text() == "✓ 校验并保存"
    assert dialog.format_button.text() == "格式化示例"


def test_settings_dialog_business_settings_source_trace_hint_copy():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    hint = dialog.strategy_help_label.text()

    assert "模板内置规则字段不会显示" not in hint
    assert hint == "高级参数可通过用户主目录下的 .ocr_extract_app/config.json 调整。"


def test_settings_dialog_business_settings_extract_strategy_has_blank_row_above():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    layout = dialog.business_settings_page.layout()
    extract_heading_index = None
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, QLabel) and widget.text() == "抽取策略":
            extract_heading_index = index
            break

    assert extract_heading_index is not None
    assert layout.itemAt(extract_heading_index - 1).spacerItem() is not None


def test_settings_dialog_restores_bottom_bar_status_after_non_closing_feedback(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    callbacks = []
    monkeypatch.setattr(
        "src.ui.settings_dialog.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )
    dialog = SettingsDialog(controller=_Controller())

    dialog._on_format_examples()

    assert dialog.bottom_bar_status_label.text() == "示例已格式化"
    assert "#19A463" in dialog.bottom_bar_status_label.styleSheet()
    assert len(callbacks) == 1
    callbacks[0]()
    assert dialog.bottom_bar_status_label.text() == "已载入配置 · config.json"
    assert dialog.bottom_bar_status_label.styleSheet() == ""


def test_settings_dialog_ignores_stale_status_restore_callback(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    callbacks = []
    monkeypatch.setattr(
        "src.ui.settings_dialog.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )
    dialog = SettingsDialog(controller=_Controller())

    dialog._on_format_examples()
    dialog.template_prompts_input.setPlainText("")
    dialog._on_save()

    assert dialog.bottom_bar_status_label.text() == "保存被阻止"
    assert len(callbacks) == 2
    callbacks[0]()
    assert dialog.bottom_bar_status_label.text() == "保存被阻止"
    callbacks[1]()
    assert dialog.bottom_bar_status_label.text() == "已载入配置 · config.json"


def test_settings_dialog_routes_save_validation_error_to_bottom_bar(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    monkeypatch.setattr("src.ui.settings_dialog.QTimer.singleShot", lambda *_args: None)
    dialog = SettingsDialog(controller=_Controller())
    dialog.template_prompts_input.setPlainText("")

    dialog._on_save()

    assert dialog.validation_error_label.isHidden() is False
    assert dialog.bottom_bar_status_label.text() == "保存被阻止"
    assert "#C9342F" in dialog.bottom_bar_status_label.styleSheet()


def test_settings_dialog_starts_save_failure_restore_after_warning_returns(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    events = []
    callbacks = []

    class _FailingSaveController(_Controller):
        def save_config(self, config):
            raise RuntimeError("无法写入配置")

    monkeypatch.setattr(
        "src.ui.settings_dialog.QMessageBox.warning",
        lambda *_args: events.append("warning"),
    )

    def _capture_single_shot(_delay, callback):
        events.append("timer")
        callbacks.append(callback)

    monkeypatch.setattr("src.ui.settings_dialog.QTimer.singleShot", _capture_single_shot)
    dialog = SettingsDialog(controller=_FailingSaveController())
    dialog.base_url_input.setText("http://localhost")
    dialog.api_key_input.setText("key")
    dialog.model_input.setEditText("model")

    dialog._on_save()

    assert events == ["warning", "timer"]
    assert dialog.bottom_bar_status_label.text() == "保存失败"
    assert "#C9342F" in dialog.bottom_bar_status_label.styleSheet()
    assert len(callbacks) == 1


def test_settings_dialog_keeps_loaded_bottom_bar_values_while_editing():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())
    loaded_model = dialog.bottom_bar_model_label.text()
    loaded_template = dialog.bottom_bar_template_label.text()
    active_template_id = dialog._pending_active_template_id

    dialog.model_input.setEditText("unsaved-model")
    new_template_id = None
    for row in range(dialog.template_list.count()):
        item = dialog.template_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) != active_template_id:
            new_template_id = item.data(Qt.ItemDataRole.UserRole)
            dialog.template_list.setCurrentRow(row)
            dialog.set_active_template_button.click()
            break

    assert dialog._pending_active_template_id != active_template_id
    assert dialog.bottom_bar_model_label.text() == loaded_model
    new_template_name = dialog._catalog.template_by_id(new_template_id).name
    assert dialog.bottom_bar_template_label.text() == new_template_name


def test_settings_dialog_ignores_status_restore_after_dialog_destroyed(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    callbacks = []
    monkeypatch.setattr(
        "src.ui.settings_dialog.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )
    dialog = SettingsDialog(controller=_Controller())

    dialog._on_format_examples()
    delete(dialog)

    callbacks[0]()


def test_settings_dialog_marks_loaded_provider_without_marking_unsaved_selection():
    app = QApplication.instance() or QApplication([])
    _ = app
    controller = _Controller()
    controller.config = replace(
        controller.config,
        provider_platform_id="deepseek",
        model="deepseek-chat",
    )
    dialog = SettingsDialog(controller=controller)

    deepseek_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "deepseek"
    )
    deepseek_row = dialog.provider_list.itemWidget(deepseek_item)
    assert deepseek_row.findChild(QLabel, "currentUseBadge").isHidden() is False
    assert dialog.provider_subtitle_label.text() == "deepseek.com"
    assert dialog.bottom_bar_provider_name_label.text() == "深度求索"
    assert dialog.bottom_bar_model_label.text() == "deepseek-chat"
    assert dialog.api_key_input.placeholderText() == "此服务商必填"

    ollama_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "ollama"
    )
    dialog.provider_list.setCurrentItem(ollama_item)
    assert dialog.api_key_input.placeholderText() == "Ollama 可选"

    custom_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "custom"
    )
    dialog.provider_list.setCurrentItem(custom_item)
    custom_row = dialog.provider_list.itemWidget(custom_item)
    assert custom_row.findChild(QLabel, "currentUseBadge").isHidden() is True
    assert dialog.provider_subtitle_label.isHidden() is True
    assert dialog.bottom_bar_provider_name_label.text() == "深度求索"
    assert dialog.api_key_input.placeholderText() == "此服务商必填"


def test_settings_dialog_marks_pending_active_template_with_badge_instead_of_star():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    dialog.template_list.setCurrentRow(1)
    dialog.set_active_template_button.click()

    item = dialog.template_list.item(1)
    row = dialog.template_list.itemWidget(item)
    assert item.text().startswith("★") is False
    assert row.findChild(QLabel, "currentUseBadge").isHidden() is False


def test_settings_dialog_connection_button_shows_pending_state(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    seen_kwargs = {}

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    class _ConnectionController(_Controller):
        def test_connection(self, **kwargs):
            seen_kwargs.update(kwargs)
            return ConnectionCheckResult(ok=True, detail="连接正常")

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_ConnectionController())
    dialog.base_url_input.setText("https://api.example.com/v1")
    dialog.api_key_input.setText("sk-test")
    dialog.model_input.setEditText("wrong-model")

    dialog._on_test_connection()

    assert dialog.test_connection_button.isEnabled() is False
    assert "检测中…" in dialog.test_connection_button.text()
    assert dialog._spinner_timer.isActive() is True
    assert pool.runnable is not None

    pool.runnable.run()
    app.processEvents()

    assert seen_kwargs["model"] == "wrong-model"
    assert dialog._active_connection_checks == set()
    assert dialog._spinner_timer.isActive() is False
    assert dialog.test_connection_button.isEnabled() is True
    assert dialog.test_connection_button.text() == "✓ 连接成功"


def test_settings_dialog_connection_ok_with_model_warning_shows_warning(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_Controller())
    dialog.base_url_input.setText("https://api.example.com/v1")
    dialog.api_key_input.setText("sk-test")
    dialog.model_input.setEditText("wrong-model")

    dialog._on_test_connection()

    pool.runnable.signals.finished.emit(
        ConnectionCheckResult(
            ok=True,
            detail="连接正常",
            model_warning="模型「wrong-model」不在可用列表中，请确认名称是否正确。",
        )
    )
    app.processEvents()

    assert dialog.test_connection_button.text() == "⚠ 模型名待确认"
    assert "#F5A524" in dialog.test_connection_button.styleSheet()
    assert "不在可用列表中" in dialog.test_connection_button.toolTip()


def test_settings_dialog_connection_ok_without_model_warning_shows_success(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_Controller())
    dialog.base_url_input.setText("https://api.example.com/v1")
    dialog.api_key_input.setText("sk-test")

    dialog._on_test_connection()

    pool.runnable.signals.finished.emit(
        ConnectionCheckResult(ok=True, detail="连接正常", model_warning="")
    )
    app.processEvents()

    assert dialog.test_connection_button.text() == "✓ 连接成功"
    assert "#19A463" in dialog.test_connection_button.styleSheet()
    assert dialog.test_connection_button.toolTip() == "连接正常"


def test_settings_dialog_real_threadpool_connection_result_updates_button():
    app = QApplication.instance() or QApplication([])
    _ = app

    class _SlowConnectionController(_Controller):
        def test_connection(self, **kwargs):
            time.sleep(0.05)
            return ConnectionCheckResult(ok=True, detail="连接正常")

    dialog = SettingsDialog(controller=_SlowConnectionController())
    dialog.base_url_input.setText("https://api.example.com/v1")
    dialog.api_key_input.setText("sk-test")

    dialog._on_test_connection()

    deadline = time.monotonic() + 1
    while dialog.test_connection_button.text() != "✓ 连接成功" and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    QThreadPool.globalInstance().waitForDone(1000)
    app.processEvents()

    assert dialog.test_connection_button.text() == "✓ 连接成功"
    assert dialog.test_connection_button.toolTip() == "连接正常"
    assert dialog._spinner_timer.isActive() is False
    assert dialog._active_connection_checks == set()


def test_settings_dialog_real_threadpool_fetch_models_updates_model_options():
    app = QApplication.instance() or QApplication([])
    _ = app

    class _SlowModelController(_Controller):
        def fetch_models(self, **kwargs):
            time.sleep(0.05)
            return ModelFetchResult(ok=True, models=("remote-model",), error="")

    dialog = SettingsDialog(controller=_SlowModelController())
    deepseek_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "deepseek"
    )
    dialog.provider_list.setCurrentItem(deepseek_item)
    dialog.api_key_input.setText("sk-test")

    dialog._on_fetch_models()

    deadline = time.monotonic() + 1
    while "remote-model" not in [dialog.model_input.itemText(i) for i in range(dialog.model_input.count())]:
        app.processEvents()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    QThreadPool.globalInstance().waitForDone(1000)
    app.processEvents()

    assert "remote-model" in [dialog.model_input.itemText(i) for i in range(dialog.model_input.count())]
    assert dialog.fetch_models_button.isEnabled() is True
    assert dialog.fetch_models_button.text() == "获取模型列表"
    assert dialog._active_model_fetches == set()


def test_settings_dialog_ignores_connection_result_after_provider_switch(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_Controller())
    deepseek_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "deepseek"
    )
    ollama_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "ollama"
    )

    dialog.provider_list.setCurrentItem(deepseek_item)
    dialog.api_key_input.setText("sk-test")
    dialog._on_test_connection()
    dialog.provider_list.setCurrentItem(ollama_item)
    pool.runnable.signals.finished.emit(ConnectionCheckResult(ok=True, detail="旧服务商结果"))
    app.processEvents()

    assert dialog.editing_provider_platform_id == "ollama"
    assert dialog.test_connection_button.text() == "✓ 检测连接"
    assert dialog.test_connection_button.toolTip() == ""
    assert dialog.test_connection_button.styleSheet() == ""


def test_settings_dialog_ignores_connection_result_after_base_url_changes(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_Controller())
    dialog.base_url_input.setText("https://api.example.com/v1")
    dialog.api_key_input.setText("sk-test")

    dialog._on_test_connection()
    dialog.base_url_input.setText("https://new.example.com/v1")
    pool.runnable.signals.finished.emit(ConnectionCheckResult(ok=False, detail="旧 timeout 详情"))
    app.processEvents()

    assert dialog.test_connection_button.text() == "✓ 检测连接"
    assert dialog.test_connection_button.toolTip() == ""
    assert dialog.test_connection_button.styleSheet() == ""


def test_settings_dialog_keeps_ollama_localhost_base_url_when_connection_times_out(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app
    timeout_detail = (
        "连接超时（7 秒）。WSL 中 localhost 指向 WSL 环境；"
        "如果 Ollama 跑在 Windows，请填写 Windows 主机 IP。"
    )

    class _TimeoutController(_Controller):
        seen_base_url = ""

        def test_connection(self, **kwargs):
            self.seen_base_url = kwargs["base_url"]
            return ConnectionCheckResult(ok=False, detail=timeout_detail)

    class _Pool:
        def start(self, runnable):
            runnable.run()

    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: _Pool(),
    )
    controller = _TimeoutController()
    dialog = SettingsDialog(controller=controller)
    ollama_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "ollama"
    )
    dialog.provider_list.setCurrentItem(ollama_item)
    dialog.base_url_input.setText("http://localhost:11434")

    dialog._on_test_connection()
    app.processEvents()

    assert controller.seen_base_url == "http://localhost:11434"
    assert dialog.base_url_input.text() == "http://localhost:11434"
    assert dialog.test_connection_button.text() == "✗ 连接失败"
    assert dialog.test_connection_button.toolTip() == timeout_detail


def test_settings_dialog_fetch_models_timeout_error_uses_provider_error_label():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())
    timeout_error = "获取模型列表超时（12 秒），请检查网络；也可以手动输入模型名。"
    ollama_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "ollama"
    )
    dialog.provider_list.setCurrentItem(ollama_item)

    dialog._fetching_models_platform_id = "ollama"
    dialog.fetch_models_button.setEnabled(False)
    dialog.fetch_models_button.setText("获取中...")
    dialog._on_fetch_models_done(
        "ollama",
        ModelFetchResult(ok=False, models=(), error=timeout_error),
    )

    assert dialog.fetch_models_button.isEnabled() is True
    assert dialog.fetch_models_button.text() == "获取模型列表"
    assert dialog.provider_fetch_error_label.isHidden() is False
    assert dialog.provider_fetch_error_label.text() == timeout_error


def test_settings_dialog_connection_check_requires_api_key_before_network_for_catalog_provider(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _ = app

    class _Pool:
        runnable = None

        def start(self, runnable):
            self.runnable = runnable

    pool = _Pool()
    monkeypatch.setattr(
        "src.ui.settings_dialog.QThreadPool.globalInstance",
        lambda: pool,
    )
    dialog = SettingsDialog(controller=_Controller())
    deepseek_item = next(
        dialog.provider_list.item(row)
        for row in range(dialog.provider_list.count())
        if dialog.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == "deepseek"
    )
    dialog.provider_list.setCurrentItem(deepseek_item)
    dialog.api_key_input.setText("")

    dialog._on_test_connection()

    assert pool.runnable is None
    assert dialog.test_connection_button.isEnabled() is True
    assert dialog.test_connection_button.text() == "✗ 连接失败"
    assert dialog.test_connection_button.toolTip() == "请先填写 API Key 后再检测连接。"


def test_settings_dialog_connection_button_uses_text_and_color_feedback():
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog = SettingsDialog(controller=_Controller())

    assert isinstance(dialog.test_connection_button, QPushButton)
    assert dialog.test_connection_button.text() == "✓ 检测连接"

    dialog._spinner_timer.start()
    dialog._on_test_connection_done(ConnectionCheckResult(ok=True, detail="连接正常"))
    assert dialog._spinner_timer.isActive() is False
    assert dialog.test_connection_button.text() == "✓ 连接成功"
    assert "#19A463" in dialog.test_connection_button.styleSheet()
    assert dialog.test_connection_button.toolTip() == "连接正常"

    dialog._on_test_connection_done(ConnectionCheckResult(ok=False, detail="连接失败详情"))
    assert dialog.test_connection_button.text() == "✗ 连接失败"
    assert "#C9342F" in dialog.test_connection_button.styleSheet()

    dialog._spinner_timer.start()
    dialog._reset_test_connection_status()
    assert dialog._spinner_timer.isActive() is False
    assert dialog.test_connection_button.text() == "✓ 检测连接"
    assert dialog.test_connection_button.styleSheet() == ""
    assert dialog.test_connection_button.toolTip() == ""


def _make_online_ocr_dialog(tmp_path):
    from src.ui.settings_dialog import SettingsController

    store = ConfigStore(config_dir=tmp_path)
    config = store.default_config()
    store.save(config)
    controller = SettingsController(config_store=store)
    return SettingsDialog(controller=controller), store


def test_ocr_page_enabled_follows_use_online(tmp_path):
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog, _store = _make_online_ocr_dialog(tmp_path)

    dialog.ocr_use_online_checkbox.setChecked(True)
    assert dialog.ocr_detail_panel.isEnabled() is True
    dialog.ocr_use_online_checkbox.setChecked(False)
    assert dialog.ocr_detail_panel.isEnabled() is False


def test_ocr_page_loads_profile(tmp_path):
    app = QApplication.instance() or QApplication([])
    _ = app
    from src.ui.settings_dialog import SettingsController

    store = ConfigStore(config_dir=tmp_path)
    config = store.default_config()
    config.ocr_use_online = True
    config.ocr_online_profiles["baidu_paddle"] = {
        "base_url": "https://x/api/v2/ocr/jobs",
        "api_key": "tk",
        "model": "PP-OCRv5",
    }
    store.save(config)
    controller = SettingsController(config_store=store)
    dialog = SettingsDialog(controller=controller)

    assert dialog.ocr_use_online_checkbox.isChecked() is True
    assert dialog.ocr_detail_panel.isEnabled() is True
    assert dialog.ocr_base_url_input.text() == "https://x/api/v2/ocr/jobs"
    assert dialog.ocr_api_key_input.text() == "tk"
    assert dialog.ocr_model_input.currentText() == "PP-OCRv5"


def test_ocr_page_collects_online_fields_into_form_data(tmp_path):
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog, _store = _make_online_ocr_dialog(tmp_path)

    dialog.base_url_input.setText("http://localhost")
    dialog.api_key_input.setText("key")
    dialog.model_input.setEditText("model")

    dialog.ocr_use_online_checkbox.setChecked(True)
    dialog.ocr_base_url_input.setText("https://x/api/v2/ocr/jobs")
    dialog.ocr_api_key_input.setText("tk")
    dialog.ocr_model_input.setEditText("PP-OCRv5")

    ok, result = dialog._build_validated_config()

    assert ok is True, result
    assert not isinstance(result, str)
    assert result.ocr_use_online is True
    assert result.ocr_online_platform_id == "baidu_paddle"
    assert result.ocr_online_profiles["baidu_paddle"]["base_url"] == "https://x/api/v2/ocr/jobs"
    assert result.ocr_online_profiles["baidu_paddle"]["api_key"] == "tk"
    assert result.ocr_online_profiles["baidu_paddle"]["model"] == "PP-OCRv5"


def test_ocr_page_nav_alignment_unchanged(tmp_path):
    app = QApplication.instance() or QApplication([])
    _ = app
    dialog, _store = _make_online_ocr_dialog(tmp_path)

    assert dialog.settings_stack.indexOf(dialog.ocr_model_page) == 1
    assert dialog.settings_stack.indexOf(dialog.template_page) == 2
    assert dialog.settings_stack.indexOf(dialog.business_settings_page) == 3
    dialog.settings_nav.setCurrentRow(1)
    assert dialog.settings_stack.currentWidget() is dialog.ocr_model_page
