from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PACKAGED_MODEL_DIRS = (
    "PP-OCRv5_mobile_det",
    "PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_textline_ori",
    "PP-LCNet_x1_0_doc_ori",
)

# Read-only asset dirs under data/ that the app loads at runtime from the bundle
# (_MEIPASS/data/...): fonts and the toolbar/queue icon sets.
PACKAGED_DATA_DIRS = (
    "fonts",
    "icons",
    "icon_1rfurz1zeyz",
)

# Read-only asset dirs under src/ui/assets/ that the app loads at runtime:
# - icons: SVG toolbar/control icons + multi-size app_icon PNGs
# - providers: per-provider LLM logos loaded by the settings dialog
PACKAGED_UI_ASSET_DIRS = (
    "icons",
    "providers",
)

# Multi-resolution icon embedded into the .exe itself (Explorer/taskbar). The
# in-app window icon uses the OLE_*.png set; this .ico keeps the file icon sharp.
EXE_ICON_PATH = "src/ui/assets/icons/app_icon/OLE.ico"

# Packages with native libs + data files that PyInstaller only collects fully
# via --collect-all; without these the frozen app crashes when running OCR.
COLLECT_ALL_PACKAGES = (
    "paddle",
    "paddleocr",
    "paddlex",
)


def build_pyinstaller_command(*, app_name: str = "OCRExtract") -> list[str]:
    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        app_name,
        "--icon",
        EXE_ICON_PATH,
    ]
    for package in COLLECT_ALL_PACKAGES:
        cmd.extend(["--collect-all", package])
    for model_name in PACKAGED_MODEL_DIRS:
        cmd.extend(["--add-data", f"models/{model_name};models/{model_name}"])
    for data_name in PACKAGED_DATA_DIRS:
        cmd.extend(["--add-data", f"data/{data_name};data/{data_name}"])
    for asset_name in PACKAGED_UI_ASSET_DIRS:
        cmd.extend(["--add-data", f"src/ui/assets/{asset_name};src/ui/assets/{asset_name}"])
    cmd.append("src/app.py")
    return cmd


def create_release_manifest(*, dist_root: str | Path, app_name: str, version: str) -> Path:
    root = Path(dist_root)
    app_dir = root / app_name
    exe_path = app_dir / f"{app_name}.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"Executable not found: {exe_path}")

    root.mkdir(parents=True, exist_ok=True)
    rollback_name = f"{app_name}_rollback_{version}.zip"
    rollback_path = root / rollback_name
    _create_rollback_package(source_dir=app_dir, rollback_path=rollback_path)

    manifest = {
        "app_name": app_name,
        "version": version,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "executable_path": str(exe_path.relative_to(root)),
        "rollback_package": rollback_name,
        "release_checklist": {
            "p0_tests_passed": False,
            "stability_100_items_passed": False,
            "startup_check_passed": False,
            "changelog_updated": False,
        },
    }
    manifest_path = root / f"{app_name}_release_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _create_rollback_package(*, source_dir: Path, rollback_path: Path) -> None:
    with ZipFile(rollback_path, "w", compression=ZIP_DEFLATED) as archive:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(source_dir.parent)
                archive.write(file_path, arcname=str(arcname))
