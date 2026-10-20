"""打包后 exe 的冒烟检查：启动、加载并解析一批图片、截图、正常退出。需要已登录的桌面会话。

只与本应用窗口交互：窗口放到 Z 序底层，用 PrintWindow 截取本窗口画面，不注入任何全局输入。
Tk 按真实的鼠标按键状态判断点击，所以这里不模拟点击；完整的压缩流程由 visual_check.py 在进程内驱动。

用法：.venv\\Scripts\\python.exe tests\\e2e_exe.py
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "批量图片压缩.exe"
WORK = ROOT / "e2e"
WALLPAPERS = Path(r"C:\Windows\Web\Wallpaper")
TITLE = "批量图片压缩"

ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.stdout.reconfigure(encoding="utf-8")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]


def make_samples(folder: Path) -> None:
    if folder.exists():
        return
    folder.mkdir(parents=True)
    shutil.copy(WALLPAPERS / "Spotlight" / "img14.jpg", folder / "风景 01.jpg")
    shutil.copy(WALLPAPERS / "ThemeA" / "img21.jpg", folder / "theme21.jpg")
    shutil.copy(WALLPAPERS / "MateBook" / "wallpaper_01.png", folder / "壁纸.png")
    with Image.open(WALLPAPERS / "ThemeA" / "img20.jpg") as image:
        exif = Image.Exif()
        exif[0x0112] = 6
        image.save(folder / "rotated.jpg", quality=95, exif=exif.tobytes())
    with Image.open(WALLPAPERS / "ThemeA" / "img22.jpg") as image:
        image.save(folder / "photo.webp", quality=95)
    with Image.open(WALLPAPERS / "ThemeA" / "img23.jpg") as image:
        image.convert("RGB").resize((1600, 1000)).save(folder / "scan.bmp")
    logo = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((40, 40, 472, 472), fill=(47, 96, 216, 255))
    logo.save(folder / "logo.png")


def find_window(timeout: float = 40) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW("TkTopLevel", TITLE)
        if hwnd and user32.IsWindowVisible(hwnd):
            return hwnd
        time.sleep(0.1)
    raise RuntimeError("app window did not appear")


def send_to_back(hwnd: int) -> None:
    """放到 Z 序最底层：窗口仍在屏幕内正常绘制，但不遮挡用户正在使用的窗口。"""
    user32.SetWindowPos(hwnd, wintypes.HWND(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def capture(hwnd: int) -> Image.Image:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("window is gone")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        user32.PrintWindow(hwnd, memory_dc, 2)
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def main() -> int:
    if not EXE.exists():
        print("exe not found; run build.py first")
        return 1
    samples = WORK / "samples"
    make_samples(samples)
    appdata = WORK / "appdata-smoke"
    shutil.rmtree(appdata, ignore_errors=True)
    settings = appdata / TITLE / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"output_dir": str(WORK / "out-smoke")}, ensure_ascii=False), encoding="utf-8")

    process = subprocess.Popen([str(EXE), str(samples)], env={**os.environ, "APPDATA": str(appdata)})
    started = time.monotonic()
    failures = []
    try:
        hwnd = find_window()
        send_to_back(hwnd)
        print(f"window after {time.monotonic() - started:.1f}s")
        time.sleep(4)
        if user32.IsHungAppWindow(hwnd):
            failures.append("window is not responding")
        shot = WORK / "exe-loaded.png"
        capture(hwnd).save(shot)
        print("screenshot:", shot)
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            failures.append("process had to be killed")
    if process.returncode != 0:
        failures.append(f"exit code {process.returncode}")
    print("FAILURES: " + "; ".join(failures) if failures else "SMOKE CHECK PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
