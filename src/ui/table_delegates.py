"""任务队列和结果预览表格的自定义 delegate。"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from src.ui.theme import COLORS

PROGRESS_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_COLOR_ROLE = int(Qt.ItemDataRole.UserRole) + 2


def _background_for(option: QStyleOptionViewItem, index: QModelIndex) -> QColor:
    is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
    is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
    if is_selected or is_hover:
        return QColor(COLORS["primary_bg"])
    if index.row() % 2 == 1:
        return QColor(COLORS["surface_soft"])
    return QColor(COLORS["canvas"])


def _content_option(option: QStyleOptionViewItem) -> QStyleOptionViewItem:
    content_option = QStyleOptionViewItem(option)
    content_option.state &= ~QStyle.StateFlag.State_Selected
    content_option.state &= ~QStyle.StateFlag.State_MouseOver
    return content_option


class TaskQueueDelegate(QStyledItemDelegate):
    """任务队列表格：选中行左侧 2px violet 竖线 + 交替行色。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        rect = QRectF(option.rect)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        painter.save()
        painter.fillRect(rect, _background_for(option, index))
        painter.restore()

        super().paint(painter, _content_option(option), index)

        if is_selected and index.column() == 0:
            painter.save()
            bar = QRectF(rect.left(), rect.top(), 2, rect.height())
            painter.fillRect(bar, QColor(COLORS["primary"]))
            painter.restore()


class ResultTableDelegate(QStyledItemDelegate):
    """结果预览表格：左侧 3px 状态色带 + 交替行色。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        rect = QRectF(option.rect)

        painter.save()
        painter.fillRect(rect, _background_for(option, index))
        painter.restore()

        super().paint(painter, _content_option(option), index)

        if index.column() == 0:
            status_color = index.data(STATUS_COLOR_ROLE)
            if status_color and isinstance(status_color, str):
                painter.save()
                bar = QRectF(rect.left(), rect.top(), 3, rect.height())
                painter.fillRect(bar, QColor(status_color))
                painter.restore()


class ProgressDelegate(QStyledItemDelegate):
    """进度列：处理中的行绘制 primary_tint 渐变条。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        rect = QRectF(option.rect)

        painter.save()
        painter.fillRect(rect, _background_for(option, index))
        painter.restore()

        progress = index.data(PROGRESS_ROLE)
        if isinstance(progress, float) and 0.0 < progress < 1.0:
            painter.save()
            grad_rect = QRectF(rect.left(), rect.top(), rect.width() * progress, rect.height())
            gradient = QLinearGradient(grad_rect.topLeft(), grad_rect.topRight())
            start = QColor(COLORS["primary_tint"])
            start.setAlphaF(0.45)
            end = QColor(COLORS["primary_tint"])
            end.setAlphaF(0.18)
            gradient.setColorAt(0.0, start)
            gradient.setColorAt(1.0, end)
            painter.fillRect(grad_rect, gradient)
            painter.restore()

        super().paint(painter, _content_option(option), index)
