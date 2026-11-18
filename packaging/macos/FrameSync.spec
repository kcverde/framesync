from importlib.metadata import distribution, version
from pathlib import Path
import sysconfig

from PyInstaller.utils.hooks import collect_data_files

here = Path(SPECPATH)
root = here.parents[1]
data = collect_data_files("framesync") + [
    (str(root / "LICENSE"), "notices/FrameSync"),
    (str(here / "notices"), "notices/Qt"),
]
for package in ("PySide6-Essentials", "shiboken6", "Pillow", "samsungtvws", "websocket-client",
                "requests", "certifi", "urllib3", "charset-normalizer", "idna", "yarl", "propcache", "multidict", "PyInstaller"):
    dist = distribution(package)
    for file in dist.files or ():
        if "license" in str(file).lower() and ".dist-info/" in str(file) and file.name != "RECORD":
            data.append((str(dist.locate_file(file)), f"notices/{package}"))

python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
data.append((str(python_license), "notices/Python"))

analysis = Analysis(
    [str(here / "launch.py")],
    pathex=[str(root / "src")],
    datas=data,
    hiddenimports=["PySide6.QtQuick", "PySide6.QtQuickControls2", "PySide6.QtSvg"],
)
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="FrameSync", console=False)
files = COLLECT(exe, analysis.binaries, analysis.datas, name="FrameSync")
app = BUNDLE(
    files,
    name="FrameSync.app",
    icon=str(here / "FrameSync.icns"),
    bundle_identifier="local.framesync.FrameSync",
    version=version("framesync"),
    info_plist={
        "CFBundleVersion": version("framesync"),
        "CFBundleShortVersionString": version("framesync"),
        "NSLocalNetworkUsageDescription": "FrameSync connects to your Samsung Frame TV to display and sync artwork.",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.photography",
    },
)
