# -*- mode: python ; coding: utf-8 -*-
"""
程序说明：PyInstaller 打包规格文件，将 cast_server.py、web/ 前端资源与 .env.example 打包为单文件 CastServer.exe。
"""

a = Analysis(
    ['cast_server.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('.env.example', '.')],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'pytest', 'unittest'],
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
    name='CastServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
