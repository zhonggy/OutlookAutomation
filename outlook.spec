# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：生成 Windows 控制台程序 OutlookAutomation.exe（onedir 模式）
# 用法：pyinstaller outlook.spec --distpath release --workpath build/pyi --noconfirm

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 程序运行时需要的外部文件（相对包内 __file__ 定位）
datas = [
    ("config/config.yaml", "config"),
    ("api/static/index.html", "api/static"),
]

hiddenimports = []

# 这些包有动态导入 / 运行时才发现的数据文件，全部收集，保证打包后不踩坑
_COLLECT = [
    "patchright",   # 自带 driver 与 py.typed
    "fastapi",
    "uvicorn",      # 动态发现 protocol loops
    "pydantic",
    "yaml",
    "apscheduler",
]
for _p in _COLLECT:
    hiddenimports += collect_submodules(_p)
    datas += collect_data_files(_p)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tests",
        "tkinter",
        "PyQt5",
        "PySide2",
        "IPython",
        "jupyter",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OutlookAutomation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    upx=False,
    upx_exclude=[],
    name="OutlookAutomation",
)