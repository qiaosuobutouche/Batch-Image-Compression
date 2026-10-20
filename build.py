"""打包成单个 exe：.venv\\Scripts\\python.exe build.py"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE_NAME = "批量图片压缩.exe"


def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)

    import PyInstaller.__main__

    icon = ROOT / "assets" / "app.ico"
    PyInstaller.__main__.run([
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", "PhotoCompressor",
        "--icon", str(icon),
        "--add-data", f"{icon}{os.pathsep}assets",
        "--version-file", str(ROOT / "version_info.txt"),
        "--collect-data", "sv_ttk",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "app.py"),
    ])

    final = ROOT / "dist" / EXE_NAME
    shutil.move(ROOT / "dist" / "PhotoCompressor.exe", final)
    print(f"已生成：{final}")


if __name__ == "__main__":
    main()
