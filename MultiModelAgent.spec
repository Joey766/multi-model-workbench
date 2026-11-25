# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

source = Path(SPECPATH) / 'src'


a = Analysis(
    [str(source / 'qt_workbench.py')],
    pathex=[str(source)],
    binaries=[],
    datas=[(str(source / 'desktop.ico'), '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'numpy', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MultiModelAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(source / 'desktop.ico')],
)
