"""Notion-style design tokens and QSS generators for the OCR Extract UI."""
from __future__ import annotations

import sys
from pathlib import Path


def _icons_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "src" / "ui" / "assets" / "icons"
    return Path(__file__).resolve().parent / "assets" / "icons"

COLORS = {
    "primary": "#5B3DE3",
    "primary_pressed": "#4F31D6",
    "primary_deep": "#4F31D6",
    "primary_light": "#7C5CFC",
    "primary_tint": "#F2EEFE",
    "primary_tint_border": "#E1D8FB",
    "success": "#19A463",
    "success_border": "#138952",
    "success_pressed": "#0F7A45",
    "success_bg": "#EBF8F1",
    "success_fg": "#138952",
    "success_border_soft": "#D2EFDD",
    "success_icon": "#1A7F46",
    "danger": "#C9342F",
    "danger_bg": "#FCEEEE",
    "danger_fg": "#C9342F",
    "danger_border": "#F4D6D5",
    "canvas": "#FFFFFF",
    "surface": "#FAFAFB",
    "surface_alt": "#F4F4F6",
    "surface_soft": "#FCFCFD",
    "surface_subtle": "#FCFCFD",
    "surface_strong": "#F7F7F8",
    "border": "#ECECF0",
    "border_input": "#E0E0E5",
    "border_dashed": "#D8D8DE",
    "separator": "#E4E4E9",
    "hairline_subtle": "#E8E8EC",
    "hairline_soft": "#E5E5EA",
    "titlebar_bg": "#F7F7F8",
    "titlebar_border": "#E8E8EC",
    "fg": "#1A1A1F",
    "fg_body": "#27272D",
    "fg_secondary": "#52525B",
    "fg_muted": "#73737A",
    "fg_faint": "#9696A0",
    "fg_disabled": "#A1A1AA",
    "on_primary": "#FFFFFF",
    "primary_bg": "#F2EEFE",
    "primary_border": "#E1D8FB",
    "primary_fg": "#5B3DE3",
    "selection_bg": "#F2EEFE",
    "selection_fg": "#5B3DE3",
    "semantic_success": "#19A463",
    "semantic_warning": "#F5A524",
    "semantic_error": "#C9342F",
    "status_ready": "#19A463",
    "status_running": "#5B3DE3",
    "status_error": "#C9342F",
    "status_idle": "#9696A0",
    "titlebar_close_hover": "#C9342F",
}

TYPOGRAPHY = {
    "family": '"Geist", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif',
    "mono": '"JetBrains Mono", ui-monospace, monospace',
    "panel_title": ("12px", "600"),
    "button": ("12.5px", "500"),
    "body": ("13px", "400"),
    "caption": ("12px", "400"),
    "micro": ("11.5px", "500"),
    "micro_small": ("11px", "500"),
    "stat_value": ("13px", "700"),
}

RADIUS = {
    "xs": "4px",
    "sm": "6px",
    "md": "8px",
    "lg": "10px",
    "icon_sm": "12px",
    "icon_lg": "14px",
    "full": "9999px",
}

SPACING = {
    "xxs": "4px",
    "xs": "6px",
    "sm": "8px",
    "md": "10px",
    "lg": "12px",
    "xl": "16px",
}

STATUS_BADGE_STYLES = {
    "total": ("总任务", "#F2EEFE", "#E1D8FB", "#5B3DE3"),
    "success": ("成功", "#EBF8F1", "#D2EFDD", "#138952"),
    "failed": ("失败", "#FCEEEE", "#F4D6D5", "#C9342F"),
    "skipped": ("跳过", "#F4F4F6", "#E5E5EA", "#52525B"),
}

RESULT_STATUS_COLORS = {
    "ASSIGNED": "#19A463",
    "ASSIGNED_PARTIAL": "#19A463",
    "INFERRED": "#F5A524",
    "INFERRED_FUZZY": "#F5A524",
    "UNCERTAIN": "#9696A0",
}


def lerp_color(a: "QColor", b: "QColor", t: float) -> "QColor":
    from PySide6.QtGui import QColor as _QColor

    t = max(0.0, min(1.0, t))
    return _QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _c(key: str) -> str:
    return COLORS[key]


def _font_family() -> str:
    return TYPOGRAPHY["family"]


def generate_main_qss() -> str:
    c = _c
    radius = RADIUS
    body_size, body_weight = TYPOGRAPHY["body"]
    button_size, button_weight = TYPOGRAPHY["button"]
    caption_size, _caption_weight = TYPOGRAPHY["caption"]
    micro_size, micro_weight = TYPOGRAPHY["micro"]
    chevron = str(_icons_dir() / "chevron-down.svg").replace("\\", "/")

    return f"""
    * {{
        font-family: {_font_family()};
        font-size: {body_size};
        font-weight: {body_weight};
        color: {c("fg")};
    }}
    QMainWindow, QWidget {{
        background: {c("surface")};
    }}
    QWidget#mainRoot {{
        background: transparent;
    }}
    QWidget#mainContentContainer {{
        background: {c("canvas")};
        border-radius: {radius["lg"]};
    }}
    QWidget#customTitleBar {{
        background: {c("titlebar_bg")};
        border-bottom: 1px solid {c("titlebar_border")};
    }}
    QFrame#titleBarSeparator {{
        background: {c("titlebar_border")};
        border: none;
    }}
    QWidget#topControlBar {{
        background: transparent;
        border: none;
        border-radius: 0;
    }}
    QWidget#statusBadgeStrip {{
        background: transparent;
        border: none;
    }}
    QFrame#cardPanel,
    QFrame#consolePanel {{
        background: {c("canvas")};
        border: 1px solid {c("border")};
        border-radius: {radius["lg"]};
    }}
    QFrame#toolbarSeparator {{
        background: {c("separator")};
        border: none;
        min-width: 1px;
        max-width: 1px;
        min-height: 20px;
        max-height: 20px;
    }}
    QLabel {{
        color: {c("fg_secondary")};
        background: transparent;
    }}
    QLabel#titleBarLabel {{
        font-size: 13px;
        font-weight: 600;
        color: {c("fg")};
        background: transparent;
    }}
    QLabel#panelTitleLabel {{
        font-size: 12px;
        font-weight: 600;
        color: {c("fg_body")};
        letter-spacing: 0.4px;
    }}
    QLabel#metaLabel {{
        color: {c("fg_muted")};
        font-size: {micro_size};
        font-weight: {micro_weight};
    }}
    QLabel#statusBarLabel {{
        background: {c("surface")};
        border: 1px solid {c("border")};
        border-radius: {radius["sm"]};
        padding: 2px 6px;
        color: {c("fg_body")};
    }}
    QLabel#emptyStateIcon {{
        color: {c("border_input")};
        font-size: 34px;
        font-weight: 600;
    }}
    QLabel#emptyStateTitle {{
        color: {c("fg_secondary")};
        font-size: 13px;
        font-weight: 600;
    }}
    QLabel#emptyStateDescription {{
        color: {c("fg_muted")};
        font-size: {caption_size};
    }}
    QLineEdit,
    QTableWidget,
    QListWidget,
    QComboBox,
    QPlainTextEdit {{
        background: {c("canvas")};
        border: 1px solid {c("border_input")};
        border-radius: {radius["md"]};
        padding: 7px 10px;
        color: {c("fg")};
        selection-background-color: {c("selection_bg")};
        selection-color: {c("selection_fg")};
        alternate-background-color: {c("surface_soft")};
    }}
    QLineEdit:focus,
    QTableWidget:focus,
    QListWidget:focus,
    QComboBox:focus,
    QPlainTextEdit:focus {{
        border: 2px solid {c("primary")};
        background: {c("canvas")};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
        subcontrol-origin: padding;
        subcontrol-position: center right;
    }}
    QComboBox::down-arrow {{
        image: url("{chevron}");
        width: 10px;
        height: 6px;
    }}
    QPushButton {{
        background: {c("canvas")};
        border: 1px solid {c("border_input")};
        border-radius: {radius["sm"]};
        padding: 0 12px;
        color: {c("fg_body")};
        font-size: {button_size};
        font-weight: {button_weight};
        min-height: 28px;
    }}
    QPushButton:hover {{
        background: {c("surface")};
        border-color: {c("fg_faint")};
    }}
    QPushButton:pressed {{
        background: {c("surface_alt")};
    }}
    QPushButton:focus {{
        border: 1px solid {c("primary")};
    }}
    QPushButton#primaryButton {{
        background: {c("primary")};
        border: 1px solid {c("primary_pressed")};
        color: {c("on_primary")};
        font-size: {button_size};
        font-weight: {button_weight};
        min-height: 28px;
        padding: 0 12px;
    }}
    QPushButton#primaryButton:hover {{
        background: {c("primary_pressed")};
    }}
    QPushButton#primaryButton:pressed {{
        background: {c("primary_deep")};
    }}
    QPushButton#successButton {{
        background: {c("success")};
        border: 1px solid {c("success_border")};
        color: {c("on_primary")};
        font-size: {button_size};
        font-weight: {button_weight};
        min-height: 28px;
        padding: 0 12px;
    }}
    QPushButton#successButton:hover {{
        background: {c("success_border")};
    }}
    QPushButton#successButton:pressed {{
        background: {c("success_pressed")};
    }}
    QPushButton#secondaryButton {{
        background: transparent;
    }}
    QPushButton#iconButton {{
        background: {c("canvas")};
        border: none;
        min-width: 34px;
        max-width: 34px;
        min-height: 34px;
        max-height: 34px;
        padding: 0;
        border-radius: {radius["md"]};
    }}
    QPushButton#titleBarSettingsButton,
    QPushButton#titleBarMinButton,
    QPushButton#titleBarMaxButton,
    QPushButton#titleBarCloseButton {{
        background: transparent;
        border: none;
        border-radius: 0;
        min-width: 0;
        min-height: 0;
        padding: 0;
    }}
    QPushButton#titleBarSettingsButton:hover,
    QPushButton#titleBarMinButton:hover,
    QPushButton#titleBarMaxButton:hover {{
        background: rgba(0, 0, 0, 0.04);
    }}
    QPushButton#titleBarCloseButton:hover {{
        background: {c("titlebar_close_hover")};
    }}
    QPushButton#inspectorCopyButton {{
        background: {c("surface")};
        border: 1px solid {c("border")};
        min-width: 50px;
        max-width: 50px;
        min-height: 22px;
        max-height: 22px;
        border-radius: {radius["sm"]};
        padding: 0 4px;
        font-size: {micro_size};
        font-weight: {micro_weight};
    }}
    QPushButton#inspectorMarkdownToggle {{
        min-height: 22px;
        max-height: 22px;
    }}
    QPushButton#task_row_expand_button,
    QPushButton#task_row_control_button,
    QPushButton#task_row_delete_button {{
        background: transparent;
        border: none;
        border-radius: 4px;
        padding: 0;
        min-width: 24px;
        max-width: 24px;
        min-height: 24px;
        max-height: 24px;
    }}
    QPushButton:disabled {{
        background: {c("border")};
        color: {c("fg_faint")};
        border: 1px solid {c("border")};
    }}
    QHeaderView::section {{
        background: {c("surface_strong")};
        color: {c("fg_secondary")};
        border: none;
        border-right: 1px solid {c("border")};
        border-bottom: 1px solid {c("border")};
        padding: 7px 6px;
        font-size: 11.5px;
        font-weight: 600;
        letter-spacing: 0.2px;
    }}
    QTableWidget::item:selected,
    QListWidget::item:selected {{
        background: {c("selection_bg")};
        color: {c("selection_fg")};
    }}
    QListWidget::item {{
        padding: 4px 2px;
    }}
    QSplitter::handle {{
        background: {c("border")};
    }}
    QSplitter::handle:hover {{
        background: {c("border_input")};
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {c("border_input")};
        border-radius: 5px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c("fg_faint")};
    }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c("border_input")};
        border-radius: 5px;
        min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c("fg_faint")};
    }}
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}
    /* --- Inline 样式收敛 --- */
    QLabel#resultCountLabel {{
        background: {c("surface_alt")};
        color: {c("fg_muted")};
        font-family: {TYPOGRAPHY["mono"]};
        font-size: 11px;
        padding: 2px 8px;
        border-radius: {radius["full"]};
    }}
    QLabel#inspectorStatusBadge {{
        background: {c("canvas")};
        border: 1px solid {c("border_input")};
        color: {c("fg_muted")};
        font-family: {TYPOGRAPHY["mono"]};
        font-size: 10px;
        padding: 2px 8px;
        border-radius: {radius["full"]};
    }}
    QLabel#ocrCharCountLabel {{
        color: {c("fg_faint")};
        font-family: {TYPOGRAPHY["mono"]};
        font-size: 11px;
        background: transparent;
    }}
    QLabel#inspectorTipsLabel {{
        background: {c("primary_bg")};
        border: 1px solid {c("primary_border")};
        color: {c("primary_fg")};
        font-size: 11.5px;
        padding: 4px 10px;
        border-radius: {radius["sm"]};
    }}
    QLabel#statusDetailLabel {{
        color: {c("fg_faint")};
        font-size: 11.5px;
        background: transparent;
    }}
    QWidget#statusBarWidget {{
        background: {c("surface")};
        border-top: 1px solid {c("border")};
    }}
    /* --- 交互状态补全 --- */
    QLineEdit:hover,
    QComboBox:hover,
    QPlainTextEdit:hover {{
        border-color: {c("fg_faint")};
    }}
    QLineEdit:disabled,
    QComboBox:disabled {{
        background: {c("surface_alt")};
        color: {c("fg_faint")};
    }}
    QTableWidget::item:hover {{
        background: {c("primary_bg")};
    }}
    QWidget#taskTableCellWidget {{
        background: transparent;
    }}
    QPushButton#task_row_expand_button:hover,
    QPushButton#task_row_control_button:hover,
    QPushButton#task_row_delete_button:hover {{
        background: {c("surface_alt")};
    }}
    QCheckBox::indicator:focus {{
        border: 2px solid {c("primary")};
    }}
    QSplitter::handle:pressed {{
        background: {c("primary_border")};
    }}
    """


def generate_settings_qss() -> str:
    c = _c
    radius = RADIUS
    body_size, _body_weight = TYPOGRAPHY["body"]
    button_size, button_weight = TYPOGRAPHY["button"]
    chevron = str(_icons_dir() / "chevron-down.svg").replace("\\", "/")

    return f"""
    * {{
        font-family: {_font_family()};
        font-size: {body_size};
        color: {c("fg")};
    }}
    QDialog#settingsDialog {{
        background: {c("surface")};
    }}
    QLabel {{
        background: transparent;
        color: {c("fg_body")};
    }}
    QLineEdit,
    QPlainTextEdit,
    QComboBox,
    QSpinBox,
    QListWidget {{
        background: {c("canvas")};
        border: 1px solid {c("border_input")};
        border-radius: {radius["sm"]};
        padding: 7px 10px;
        color: {c("fg")};
        selection-background-color: {c("selection_bg")};
        selection-color: {c("selection_fg")};
    }}
    QListWidget {{
        border-radius: {radius["md"]};
        padding: 0px;
    }}
    QLineEdit:focus,
    QPlainTextEdit:focus,
    QComboBox:focus,
    QSpinBox:focus,
    QListWidget:focus {{
        border: 1px solid {c("primary_border")};
        background: {c("canvas")};
    }}
    QSpinBox {{
        min-width: 140px;
        min-height: 28px;
    }}
    QSpinBox::up-button,
    QSpinBox::down-button {{
        subcontrol-origin: border;
        width: 18px;
    }}
    QPushButton {{
        background: {c("canvas")};
        border: 1px solid {c("border_input")};
        border-radius: {radius["sm"]};
        padding: 8px 16px;
        color: {c("fg")};
        font-size: {button_size};
        font-weight: {button_weight};
        min-height: 28px;
    }}
    QPushButton:hover {{
        background: {c("surface")};
        border-color: {c("fg_faint")};
    }}
    QPushButton:pressed {{
        background: {c("surface_alt")};
    }}
    QPushButton:focus {{
        border: 1px solid {c("primary")};
    }}
    QPushButton#primaryButton {{
        background: {c("primary")};
        border: 1px solid {c("primary_pressed")};
        color: {c("on_primary")};
        font-size: {button_size};
        font-weight: {button_weight};
        min-height: 28px;
        padding: 0 12px;
    }}
    QPushButton#primaryButton:hover {{
        background: {c("primary_pressed")};
    }}
    QPushButton#primaryButton:pressed {{
        background: {c("primary_deep")};
    }}
    QPushButton#cancelButton {{
        background: transparent;
        padding: 0 12px;
    }}
    QPushButton:disabled {{
        background: {c("border")};
        color: {c("fg_faint")};
        border: 1px solid {c("border")};
    }}
    QLabel#validation_error_label {{
        color: {c("semantic_error")};
        font-weight: 600;
        background: {c("danger_bg")};
        border: 1px solid {c("danger_border")};
        border-radius: {radius["md"]};
        padding: 6px 8px;
    }}
    QLabel#statusLabel {{
        color: {c("fg_muted")};
    }}
    QCheckBox {{
        background: transparent;
        color: {c("fg_secondary")};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {c("border_input")};
        border-radius: 4px;
        background: {c("canvas")};
    }}
    QCheckBox::indicator:hover {{
        border: 1px solid {c("primary")};
    }}
    QCheckBox::indicator:checked {{
        background: {c("primary")};
        border: 1px solid {c("primary_pressed")};
    }}
    QToolButton {{
        background: transparent;
        border: none;
        border-radius: {radius["xs"]};
        padding: 4px;
    }}
    QToolButton:hover {{
        background: rgba(0, 0, 0, 0.04);
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
        subcontrol-origin: padding;
        subcontrol-position: center right;
    }}
    QComboBox::down-arrow {{
        image: url("{chevron}");
        width: 10px;
        height: 6px;
    }}
    QComboBox QLineEdit {{
        background: transparent;
        border: none;
        padding: 0;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {c("border_input")};
        border-radius: 5px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c("fg_faint")};
    }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c("border_input")};
        border-radius: 5px;
        min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c("fg_faint")};
    }}
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}
    QFrame#settingsBottomBar {{
        background: {c("surface")};
        border-top: 1px solid {c("border")};
        min-height: 52px;
        max-height: 52px;
    }}
    QLabel#currentUseBadge {{
        color: {c("primary")};
        padding: 2px 8px;
        font-size: 10px;
    }}
    QLabel#providerSubtitle,
    QLabel#bottomBarStatus {{
        color: {c("fg_faint")};
        font-size: 11px;
    }}
    QLabel#bottomBarCurrentUse {{
        color: {c("fg_muted")};
        font-size: 11px;
        font-weight: 500;
    }}
    QLabel#bottomBarProviderName {{
        color: {c("fg_secondary")};
        font-size: 11px;
        font-weight: 500;
    }}
    QLabel#settingsSectionHeading {{
        color: {c("fg_secondary")};
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.4px;
    }}
    QLabel#settingsCaption {{
        color: {c("fg_muted")};
        font-size: 11px;
    }}
    QListWidget#settingsNav {{
        background: transparent;
        border: none;
        padding: 2px 0;
    }}
    QListWidget#settingsNav::item {{
        min-height: 36px;
        margin: 4px 0;
        padding: 0 12px;
        border: none;
        border-radius: {radius["xs"]};
        color: {c("fg_secondary")};
    }}
    QListWidget#settingsNav::item:hover {{
        background: {c("surface_alt")};
        border: none;
        border-radius: {radius["xs"]};
    }}
    QListWidget#settingsNav::item:selected {{
        background: {c("primary_bg")};
        border: none;
        border-radius: {radius["xs"]};
        color: {c("primary_fg")};
    }}
    QListWidget#settingsNav::item:focus {{
        outline: none;
        border: none;
    }}
    QFrame#settingsCard {{
        background: {c("canvas")};
        border: 1px solid {c("border")};
        border-radius: {radius["md"]};
    }}
    QListWidget#providerList::item {{
        min-height: 40px;
        padding: 0;
        border: none;
    }}
    QListWidget#providerList::item:selected {{
        background: transparent;
    }}
    QListWidget#templateList::item {{
        min-height: 40px;
        padding: 0;
        border: none;
    }}
    QListWidget#templateList::item:selected {{
        background: transparent;
    }}
    QListWidget#providerList {{
        background: transparent;
        border: none;
    }}
    QListWidget#templateList {{
        background: transparent;
        border: none;
    }}
    QWidget#currentUseRow {{
        background: transparent;
        border-left: 2px solid transparent;
        border-radius: {radius["sm"]};
    }}
    QWidget#currentUseRow[selected="true"] {{
        background: {c("primary_bg")};
        border-left: none;
    }}
    """
