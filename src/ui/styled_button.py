"""QPainter-based button with subtle shadows and pixmap caching."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget

from src.ui.theme import COLORS, TYPOGRAPHY, lerp_color

_RADIUS = 6

_VARIANT_PARAMS: dict[str, dict[str, Any]] = {
    "default": {
        "bg": COLORS["canvas"],
        "fg": COLORS["fg_body"],
        "border": COLORS["border_input"],
        "hover_bg": COLORS["surface"],
        "pressed_bg": COLORS["surface_alt"],
        "inset_alpha": 0.6,
        "outer_color": QColor(20, 20, 40, int(255 * 0.03)),
        "outer_blur": 1,
    },
    "primary": {
        "bg": COLORS["primary"],
        "fg": COLORS["on_primary"],
        "border": COLORS["primary_pressed"],
        "hover_bg": COLORS["primary_pressed"],
        "pressed_bg": COLORS["primary_deep"],
        "inset_alpha": 0.2,
        "outer_color": QColor(91, 61, 227, int(255 * 0.3)),
        "outer_blur": 2,
    },
    "success": {
        "bg": COLORS["success"],
        "fg": COLORS["on_primary"],
        "border": COLORS["success_border"],
        "hover_bg": COLORS["success_border"],
        "pressed_bg": COLORS["success_pressed"],
        "inset_alpha": 0.2,
        "outer_color": QColor(25, 164, 99, int(255 * 0.3)),
        "outer_blur": 2,
    },
    "danger": {
        "bg": COLORS["canvas"],
        "fg": COLORS["danger"],
        "border": COLORS["border_input"],
        "hover_bg": COLORS["surface"],
        "pressed_bg": COLORS["surface_alt"],
        "inset_alpha": 0.6,
        "outer_color": QColor(20, 20, 40, int(255 * 0.03)),
        "outer_blur": 1,
    },
}


class _StyledButton(QPushButton):
    """A compact QPushButton rendered through QPainter for refined UI chrome."""

    def __init__(
        self,
        text: str = "",
        *,
        variant: str = "default",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        if variant not in _VARIANT_PARAMS:
            raise ValueError(f"Unknown styled button variant: {variant}")
        self._variant = variant
        self._params = _VARIANT_PARAMS[variant]
        self._hover = False
        self._pressed = False
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._anim_progress = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_anim_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setMinimumHeight(28)
        self.setFont(self._button_font())

    @staticmethod
    def _button_font() -> QFont:
        families = [f.strip().strip('"') for f in TYPOGRAPHY["family"].split(",")]
        font = QFont(families[0])
        font.setFamilies(families)
        size_str, weight_str = TYPOGRAPHY["button"]
        font.setPixelSize(round(float(size_str.replace("px", ""))))
        weight_map = {"400": QFont.Weight.Normal, "500": QFont.Weight.Medium, "600": QFont.Weight.DemiBold, "700": QFont.Weight.Bold}
        font.setWeight(weight_map.get(weight_str, QFont.Weight.Medium))
        return font

    def _invalidate_cache(self) -> None:
        if hasattr(self, "_pixmap_cache"):
            self._pixmap_cache.clear()
        self.update()

    def setText(self, text: str) -> None:
        super().setText(text)
        self._invalidate_cache()

    def setIcon(self, icon: QIcon) -> None:
        super().setIcon(icon)
        self._invalidate_cache()

    def setIconSize(self, size: Any) -> None:
        super().setIconSize(size)
        self._invalidate_cache()

    def resize(self, *args: Any) -> None:
        self._invalidate_cache()
        super().resize(*args)

    def resizeEvent(self, event: Any) -> None:
        self._invalidate_cache()
        super().resizeEvent(event)

    def _start_anim(
        self,
        start: float,
        end: float,
        duration: int,
        easing: QEasingCurve.Type,
    ) -> None:
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(easing)
        self._anim.start()

    def _on_anim_tick(self, value: Any) -> None:
        self._anim_progress = float(value)
        self._invalidate_cache()

    def enterEvent(self, event: Any) -> None:
        self._hover = True
        self._pressed = False
        self._start_anim(self._anim_progress, 1.0, 150, QEasingCurve.Type.OutCubic)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hover = False
        self._pressed = False
        self._start_anim(self._anim_progress, 0.0, 200, QEasingCurve.Type.InCubic)
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        self._pressed = True
        self._invalidate_cache()
        self._start_anim(self._anim_progress, 1.0, 50, QEasingCurve.Type.Linear)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        self._pressed = False
        self._invalidate_cache()
        super().mouseReleaseEvent(event)

    def focusInEvent(self, event: Any) -> None:
        self._invalidate_cache()
        super().focusInEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self._invalidate_cache()
        super().focusOutEvent(event)

    def _cache_key(self) -> str:
        if not self.isEnabled():
            state = "disabled"
        elif self._pressed:
            state = "pressed"
        elif self._hover:
            state = "hover"
        else:
            state = "normal"
        focus_state = "focus" if self.hasFocus() else "blur"
        return f"{self._variant}_{state}_{focus_state}_{self.width()}x{self.height()}"

    def _current_bg(self) -> QColor:
        params = self._params
        if not self.isEnabled():
            return QColor(params["bg"])
        if self._pressed:
            return QColor(params["pressed_bg"])
        if self._hover:
            base = QColor(params["bg"])
            target = QColor(params["hover_bg"])
            return lerp_color(base, target, self._anim_progress)
        return QColor(params["bg"])

    def paintEvent(self, event: Any) -> None:
        key = self._cache_key()
        if key not in self._pixmap_cache:
            pixmap = QPixmap(self.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._render(painter)
            painter.end()
            self._pixmap_cache[key] = pixmap

        painter = QPainter(self)
        if not self.isEnabled():
            painter.setOpacity(0.5)
        painter.drawPixmap(0, 0, self._pixmap_cache[key])
        painter.end()

    def _render(self, painter: QPainter) -> None:
        params = self._params
        width = self.width()
        height = self.height()
        margin = params["outer_blur"] + 1
        body = QRectF(margin, margin, width - 2 * margin, height - 2 * margin - 1)

        if self.isEnabled():
            shadow_rect = body.adjusted(0, 1, 0, 1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(params["outer_color"]))
            painter.drawRoundedRect(shadow_rect, _RADIUS, _RADIUS)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._current_bg())
        painter.drawRoundedRect(body, _RADIUS, _RADIUS)

        if self.isEnabled() and not self._pressed:
            inset = QColor(255, 255, 255, int(255 * params["inset_alpha"]))
            painter.setPen(QPen(inset, 1))
            painter.drawLine(
                int(body.left() + _RADIUS),
                int(body.top() + 1),
                int(body.right() - _RADIUS),
                int(body.top() + 1),
            )

        painter.setPen(QPen(QColor(params["border"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body, _RADIUS, _RADIUS)

        if self.hasFocus():
            painter.setPen(QPen(QColor(COLORS["primary"]), 2))
            painter.drawRoundedRect(body.adjusted(1, 1, -1, -1), _RADIUS - 1, _RADIUS - 1)

        self._paint_content(painter, body)

    def _paint_content(self, painter: QPainter, body: QRectF) -> None:
        painter.setPen(QColor(self._params["fg"]))
        painter.setFont(self.font())

        icon = self.icon()
        icon_size = self.iconSize()
        text = self.text()

        if icon and not icon.isNull() and text:
            icon_width = icon_size.width()
            gap = 6
            text_rect = painter.fontMetrics().boundingRect(text)
            total_width = icon_width + gap + text_rect.width()
            x_start = body.center().x() - total_width / 2
            icon_y = body.center().y() - icon_size.height() / 2
            icon.paint(painter, int(x_start), int(icon_y), icon_width, icon_size.height())
            text_x = x_start + icon_width + gap
            painter.drawText(
                QRectF(text_x, body.top(), body.right() - text_x, body.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
        elif icon and not icon.isNull():
            icon_x = body.center().x() - icon_size.width() / 2
            icon_y = body.center().y() - icon_size.height() / 2
            icon.paint(painter, int(icon_x), int(icon_y), icon_size.width(), icon_size.height())
        elif text:
            painter.drawText(body, Qt.AlignmentFlag.AlignCenter, text)
