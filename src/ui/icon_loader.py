from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

_ICON_SUBDIR = Path("src") / "ui" / "assets" / "icons"


def _icon_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / _ICON_SUBDIR
    return Path(__file__).resolve().parent / "assets" / "icons"


def load_icon(name: str) -> QIcon:
    path = _icon_dir() / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    return QIcon(str(path))
