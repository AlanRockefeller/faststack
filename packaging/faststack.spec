# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import importlib.metadata
import os
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


ROOT = Path(SPECPATH).parent
APP_NAME = "FastStack"
BUNDLE_ID = "dev.faststack.FastStack"


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def existing_binary(
    path: str | os.PathLike[str],
    dest: str = ".",
) -> tuple[str, str] | None:
    candidate = Path(path)
    if candidate.is_file():
        return str(candidate), dest
    return None


datas = [
    (str(ROOT / "faststack" / "qml"), "faststack/qml"),
    # Bundle the README next to the package so Help > View Readme works in
    # the frozen macOS/Windows builds (see resources.faststack_readme_path).
    (str(ROOT / "README.md"), "faststack"),
]
# Ship faststack's own dist-info so importlib.metadata.version("faststack")
# resolves inside the frozen build. Without it the updater cannot tell which
# version is running and stops offering updates entirely (see
# faststack/updater.get_current_version).
try:
    datas += copy_metadata("faststack")
except Exception as exc:  # pragma: no cover - build-time diagnostics only
    raise RuntimeError("faststack distribution metadata is required") from exc

# copy_metadata() copies whatever dist-info is *installed*, which can lag the
# checkout being frozen (a stale editable install, an old wheel in the build
# environment). Bundling that would make the frozen app report the wrong
# version and compare updates against it. Install the current checkout before
# running PyInstaller; refuse to build if the two disagree.
installed_version = importlib.metadata.version("faststack")
if installed_version != project_version():
    raise RuntimeError(
        f"Installed faststack metadata is {installed_version!r} but "
        f"pyproject.toml declares {project_version()!r}; reinstall the "
        'current checkout (pip install -e ".[bundle]") before building.'
    )

datas += collect_data_files(
    "PySide6",
    includes=[
        "Qt/qml/Qt/**",
        "Qt/qml/QtCore/**",
        "Qt/qml/QtQml/**",
        "Qt/qml/QtQuick/**",
        "Qt/qml/Qt5Compat/**",
    ],
)

binaries = []
for candidate in (
    os.environ.get("FASTSTACK_TURBOJPEG_LIB"),
    os.environ.get("TURBOJPEG_LIB"),
    "C:/libjpeg-turbo64/bin/turbojpeg.dll",
    "C:/Program Files/libjpeg-turbo/bin/turbojpeg.dll",
    "/opt/homebrew/opt/jpeg-turbo/lib/libturbojpeg.dylib",
    "/usr/local/opt/jpeg-turbo/lib/libturbojpeg.dylib",
):
    if candidate:
        binary = existing_binary(candidate)
        if binary is not None:
            binaries.append(binary)
            break

hiddenimports = [
    "PIL.ImageCms",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtWidgets",
    "cv2",
    "turbojpeg",
]

a = Analysis(
    [str(ROOT / "faststack" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": project_version(),
            "CFBundleVersion": project_version(),
            "NSHighResolutionCapable": True,
        },
    )
