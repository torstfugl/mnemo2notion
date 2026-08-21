# PyInstaller spec for the desktop app.
#
# One windowed executable, the GUI's web files carried as data. Build with:
#   pyinstaller notion2mnemo.spec
# Output lands in dist/NotionMnemoConverter.exe.

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("notion2mnemo/gui/web", "notion2mnemo/gui/web"),
    ],
    # pywebview's Windows backend loads pieces lazily; make sure they all ride.
    hiddenimports=collect_submodules("webview"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NotionMnemoConverter",
    debug=False,
    strip=False,
    upx=False,
    console=False,   # a GUI app; the CLI stays `python -m notion2mnemo`
    icon=None,
)
