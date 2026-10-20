"""在进程内驱动真实界面依次跑三种模式，并截取每轮完成后的窗口画面。

窗口放在 Z 序底层，截图只包含本应用窗口；设置文件写到临时目录，不影响真实偏好。

用法：.venv\\Scripts\\python.exe tests\\visual_check.py [--dark]
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import e2e_exe  # noqa: E402  先导入：它会在创建 Tk 之前开启 DPI 感知
import app as app_module  # noqa: E402
from engine import Mode  # noqa: E402

e2e_exe.user32.GetParent.restype = e2e_exe.wintypes.HWND
e2e_exe.user32.GetParent.argtypes = [e2e_exe.wintypes.HWND]


def pump(root, condition, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.02)
    raise RuntimeError("timed out")


def settle(root, seconds=0.8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def main() -> int:
    dark = "--dark" in sys.argv
    theme = "dark" if dark else "light"
    samples = e2e_exe.WORK / "samples"
    e2e_exe.make_samples(samples)
    out = e2e_exe.WORK / f"out-visual-{theme}"
    shutil.rmtree(out, ignore_errors=True)
    shots = e2e_exe.WORK / "visual"
    shots.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as prefs_dir, \
            mock.patch.object(app_module, "prefs_path", return_value=Path(prefs_dir) / "settings.json"), \
            mock.patch.object(app_module, "system_uses_dark_theme", return_value=dark):
        root = app_module.TkinterDnD.Tk()
        app = app_module.App(root, [str(samples)])
        app.output_dir.set(str(out))
        root.update()
        hwnd = e2e_exe.user32.GetParent(root.winfo_id())
        e2e_exe.send_to_back(hwnd)
        pump(root, lambda: len(app.items) == 7 and all(i.info or i.error for i in app.items.values()))
        settle(root)
        e2e_exe.capture(hwnd).save(shots / f"{theme}-0-loaded.png")

        for name, mode in (("1-quality", Mode.QUALITY), ("2-scale", Mode.SCALE), ("3-size", Mode.SIZE)):
            app.mode.set(mode.value)
            if mode is Mode.SCALE:
                app.scale_text.set("50")
                app._on_scale_typed()
            if mode is Mode.SIZE:
                app.width_text.set("800")
                app.height_text.set("600")
                app.keep_ratio.set(False)
            app._show_mode_panel()
            app.toggle_run()
            pump(root, lambda: not app.running)
            settle(root)
            e2e_exe.capture(hwnd).save(shots / f"{theme}-{name}.png")
            print(f"{name}: {app.status.cget('text')}")
        app.on_close()
    print("screenshots:", shots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
