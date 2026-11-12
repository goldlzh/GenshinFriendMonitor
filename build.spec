# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

生成单文件 exe（onefile）。构建命令：pyinstaller build.spec
"""
from PyInstaller.utils.hooks import collect_all
from importlib.util import find_spec
import os
import sys

datas, binaries, hiddenimports = [], [], []

# conda 环境下，标准库扩展（_ctypes/_sqlite3/_ssl/_hashlib/_bz2/_lzma/
# _decimal/pyexpat 等）依赖的第三方 DLL 位于 <conda>\Library\bin，
# PyInstaller 默认不扫描该目录，导致打包后 "DLL load failed while importing xxx"。
# 这里显式收集所需 DLL，避免运行时找不到。已用 pefile 扫描确认为标准库 pyd 的依赖：
_conda_bin = os.path.join(sys.base_prefix, "Library", "bin")
_LIBBIN_DLLS = (
    # _ctypes
    "ffi.dll", "ffi-8.dll", "ffi-7.dll",
    # _sqlite3
    "sqlite3.dll",
    # _ssl / _hashlib
    "libssl-3-x64.dll", "libcrypto-3-x64.dll",
    # _bz2 / _lzma
    "libbz2.dll", "liblzma.dll",
    # pyexpat / _elementtree（XML 解析）
    "libexpat.dll",
    # _decimal
    "libmpdec-4.dll",
)
for _dll in _LIBBIN_DLLS:
    _p = os.path.join(_conda_bin, _dll)
    if os.path.exists(_p):
        binaries.append((_p, "."))

# Use the C++ runtime shipped with Qt instead of a DLL from an unrelated PATH
# entry on the build machine (for example, a PDF/image utility environment).
_qt_dir = os.path.dirname(find_spec("PySide6").origin)
_qt_runtime = ("concrt140.dll", "msvcp140.dll", "msvcp140_1.dll",
               "msvcp140_2.dll", "msvcp140_codecvt_ids.dll",
               "vcruntime140.dll", "vcruntime140_1.dll")
for _dll in _qt_runtime:
    _p = os.path.join(_qt_dir, _dll)
    if os.path.isfile(_p):
        binaries.append((_p, "."))

# 收集 OCR 引擎及其模型、onnxruntime 运行时、以及 rapidocr 的 native 依赖
for pkg in ("rapidocr", "onnxruntime", "pyclipper", "cv2", "shapely", "omegaconf", "fontTools"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += ["PIL", "win32gui", "win32api", "win32con", "win32process", "win32security",
                  "PySide6.QtMultimedia"]
datas += [("app/assets/chat_icon.png", "app/assets")]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtWebEngineQuick", "PySide6.QtQuick", "PySide6.QtQml",
              "PySide6.QtMultimediaWidgets", "tkinter"],
    noarchive=False,
)

# Qt uses Windows' system ICU API (unversioned ucnv_* exports). A third-party
# icuuc.dll found on PATH can instead export ucnv_*_78, making QtWidgets fail
# before the window opens. Windows 10/11 provide the correct system library.
a.binaries = [entry for entry in a.binaries
              if entry[0].lower() not in {"icuuc.dll", "icudt78.dll"}]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GenshinFriendMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不显示控制台（调试时可改为 True 查看输出）
    uac_admin=True,         # Windows requests elevation before starting the EXE.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
