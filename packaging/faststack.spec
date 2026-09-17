# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import importlib.metadata
import os
import re
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

# ---------------------------------------------------------------------------
# Bundle pruning
#
# FastStack's ENTIRE Qt surface is:
#   Python : QtCore, QtGui, QtWidgets, QtQml, QtQuick
#   QML    : QtQuick, QtQuick.Controls, QtQuick.Layouts,
#            QtQuick.Controls.Material, QtQuick.Window, QtCore
# Nothing below is reachable from that set, yet PyInstaller collects it all
# through PySide6's dependency graph. Measured on the shipped 1.6.8 Windows
# build: ~125 MB of a 240 MB download, of which QtWebEngine alone is 86.8 MB
# compressed (194 MB on disk) for a module the source never mentions.
#
# Re-check this list whenever a new `import` appears in faststack/qml/*.qml or
# a new `from PySide6.X` appears in the Python sources. The build prints what
# it dropped, and never drops a library whose stem is in KEEP_EXACT.
# ---------------------------------------------------------------------------
# Every pattern is fully qualified with its Qt prefix so it cannot match by
# accident ("location" alone would hit "relocation", "allocation", ...).
UNUSED_QT = [
    "qtwebengine", "qt6webengine",
    "qt3d", "qt63d",
    "qtquick3d", "qt6quick3d",
    "qtcharts", "qt6charts",
    "qtgraphs", "qt6graphs",
    "qtdatavisualization", "qt6datavisualization",
    "qtmultimedia", "qt6multimedia",
    "qtpdf", "qt6pdf",
    "qtlocation", "qt6location",
    "qtpositioning", "qt6positioning",
    "qtsensors", "qt6sensors",
    "qtbluetooth", "qt6bluetooth",
    "qtnfc", "qt6nfc",
    "qtserialport", "qt6serialport", "qtserialbus", "qt6serialbus",
    "qtremoteobjects", "qt6remoteobjects",
    "qtspatialaudio", "qt6spatialaudio",
    "qttexttospeech", "qt6texttospeech",
    "qtscxml", "qt6scxml",
    "qtstatemachine", "qt6statemachine",
    "qtsql", "qt6sql",
    "qttest", "qt6test",
    "qtdesigner", "qt6designer",
    "qthelp", "qt6help",
    "qtwebsockets", "qt6websockets",
    "qtwebchannel", "qt6webchannel",
]
# Non-Qt weight: OpenCV's video reader (FastStack opens no video), Pillow's
# AVIF codec (no AVIF support in the app), and Tcl/Tk (no tkinter anywhere).
UNUSED_OTHER = [
    "opencv_videoio_ffmpeg", "pil/_avif",
    "_tcl_data", "_tk_data", "tcl8", "tk8.", "_tkinter", "libtcl", "libtk",
]
# Exact library stems that must survive whatever the patterns above say.
# Substring matching would be wrong here: "qt6quick" is a substring of
# "qt6quick3d", so a substring keep-guard would protect the very module the
# pattern list is trying to drop. opengl32sw is the software OpenGL fallback
# that keeps FastStack running on a machine with no usable GPU driver -- 20 MB,
# and it stays.
KEEP_EXACT = {
    "qt6core", "qt6gui", "qt6widgets", "qt6qml", "qt6quick", "qt6network",
    "qt6opengl", "qt6openglwidgets", "qt6svg", "qt6svgwidgets", "qt6dbus",
    "qt6qmlmodels", "qt6qmlworkerscript", "qt6qmlmeta", "qt6quickcontrols2",
    "qt6quicktemplates2", "qt6quicklayouts", "qt6quickcontrols2impl",
    "qt6quickdialogs2", "qt6quickdialogs2quickimpl", "qt6quickdialogs2utils",
    "qt6quickeffects", "qt6quickshapes", "qt6quickparticles",
    "qtcore", "qtgui", "qtwidgets", "qtqml", "qtquick", "qtnetwork",
    "qtopengl", "qtsvg", "qtquickcontrols2", "qtquickwidgets",
    "opengl32sw", "libopengl32sw",
}


def _stem(path):
    """Library stem: basename, lowercased, with every extension removed
    (handles libfoo.so.6.5.0 as well as Foo.dll and Foo.cp313-win_amd64.pyd)."""
    base = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    # strip the macOS/Linux "lib" prefix so one keep-list covers all platforms
    if base.startswith("lib") and not base.startswith("libjpeg"):
        base = base[3:]
    return re.split(r"\.(?:so|dll|dylib|cp\d+|pyd|abi\d+|\d+)\b", base)[0]


def prune_bundle(entries, label):
    """Drop unreachable Qt modules and dead codecs from a TOC."""
    patterns = UNUSED_QT + UNUSED_OTHER
    kept, dropped = [], []
    for entry in entries:
        name = str(entry[0]).replace("\\", "/").lower()
        if _stem(name) in KEEP_EXACT:
            kept.append(entry)
            continue
        hit = next((p for p in patterns if p in name), None)
        if hit:
            dropped.append((entry, name, hit))
        else:
            kept.append(entry)
    if dropped:
        by_pattern = {}
        for entry, name, hit in dropped:
            try:
                size = os.path.getsize(entry[1])
            except (OSError, TypeError, IndexError):
                size = 0
            slot = by_pattern.setdefault(hit, [0, 0])
            slot[0] += 1
            slot[1] += size
        total = sum(v[1] for v in by_pattern.values())
        print(
            f"[faststack] pruned {len(dropped)} unreachable {label} entries, "
            f"{total / 1e6:.1f} MB uncompressed"
        )
        for hit in sorted(by_pattern, key=lambda h: -by_pattern[h][1]):
            count, size = by_pattern[hit]
            if size > 200_000:
                print(f"[faststack]     {hit:24s} {count:>4d} files {size / 1e6:7.1f} MB")
    return kept


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

EXCLUDED_MODULES = [
    "tkinter",
    "_tkinter",
    "Tkinter",
    "pydoc_data",
] + [
    f"PySide6.Qt{name}"
    for name in (
        "WebEngineCore", "WebEngineWidgets", "WebEngineQuick", "WebChannel",
        "WebSockets", "Multimedia", "MultimediaWidgets", "Charts", "Graphs",
        "DataVisualization", "Pdf", "PdfWidgets", "Location", "Positioning",
        "Sensors", "Bluetooth", "Nfc", "SerialPort", "SerialBus",
        "RemoteObjects", "SpatialAudio", "TextToSpeech", "Scxml",
        "StateMachine", "Sql", "Test", "Designer", "Help", "Quick3D",
        "3DCore", "3DRender", "3DInput", "3DLogic", "3DAnimation",
        "3DExtras",
    )
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
    excludes=EXCLUDED_MODULES,
    noarchive=False,
    optimize=0,
)
a.binaries = prune_bundle(a.binaries, "binary")
a.datas = prune_bundle(a.datas, "data")

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
