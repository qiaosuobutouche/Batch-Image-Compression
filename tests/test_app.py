import gc
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from tkinterdnd2 import TkinterDnD

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
from engine import Mode  # noqa: E402
from test_engine import photo  # noqa: E402


class AppTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

        prefs = mock.patch.object(app_module, "prefs_path", return_value=self.dir / "prefs" / "settings.json")
        prefs.start()
        self.addCleanup(prefs.stop)

        self.messages = []
        for name in ("showinfo", "showwarning", "showerror"):
            patcher = mock.patch.object(
                app_module.messagebox, name,
                side_effect=lambda *args, _name=name, **kwargs: self.messages.append((_name, args)))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.root = TkinterDnD.Tk()
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.app = app_module.App(self.root)
        self.out = self.dir / "out"
        self.app.output_dir.set(str(self.out))

    def _destroy_root(self):
        # 测试会反复创建 Tk；必须在主线程里回收旧窗口，否则循环 GC 可能在工作线程中析构 Tk 对象。
        for thread in threading.enumerate():
            if thread is not threading.main_thread():
                thread.join(timeout=30)
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.app = None
        gc.collect()

    def pump(self, condition, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for the UI")

    def make_folder(self):
        src = self.dir / "src"
        (src / "nested").mkdir(parents=True)
        photo(1200, 900, 1).save(src / "a.jpg", quality=95)
        photo(800, 600, 2).save(src / "nested" / "b.png")
        photo(640, 480, 3).save(src / "c photo.webp", quality=95)
        (src / "broken.jpg").write_bytes(b"not an image")
        (src / "readme.txt").write_text("x")
        return src

    def add_and_wait(self, paths, expected):
        self.app.add_paths([str(p) for p in paths])
        self.pump(lambda: len(self.app.items) == expected
                  and all(item.info or item.error for item in self.app.items.values()))

    def run_batch(self):
        self.app.toggle_run()
        self.assertTrue(self.app.running)
        self.pump(lambda: not self.app.running)

    def results(self):
        return {item.path.name: item.result for item in self.app.items.values() if item.result}

    def status_of(self, name):
        for iid, item in self.app.items.items():
            if item.path.name == name:
                return self.app.tree.item(iid, "values")[5]
        raise KeyError(name)


class BatchTests(AppTestCase):
    def test_folder_batch_in_all_three_modes(self):
        src = self.make_folder()
        self.add_and_wait([src], expected=4)
        self.assertIn("无法读取", self.status_of("broken.jpg"))
        self.assertEqual(self.app.tree.item(self.app.keys[app_module.engine.path_key(src / "a.jpg")], "values")[1],
                         "1200 × 900")

        self.app.mode.set(Mode.QUALITY.value)
        self.app.quality_index.set(1)
        self.app._show_mode_panel()
        self.run_batch()
        results = self.results()
        self.assertEqual(sorted(results), ["a.jpg", "b.png", "c photo.webp"])
        for name, size in (("a.jpg", (1200, 900)), ("b.png", (800, 600)), ("c photo.webp", (640, 480))):
            with Image.open(results[name].output) as out:
                self.assertEqual(out.size, size)
        self.assertTrue(self.status_of("a.jpg").startswith("完成"))
        self.assertEqual(self.app.run_stats["failed"], 0)
        self.assertIn("共节省", self.app.status.cget("text"))

        self.app.mode.set(Mode.SCALE.value)
        self.app.scale_text.set("25")
        self.run_batch()
        sizes = {name: (r.width, r.height) for name, r in self.results().items()}
        self.assertEqual(sizes, {"a.jpg": (300, 225), "b.png": (200, 150), "c photo.webp": (160, 120)})

        self.app.mode.set(Mode.SIZE.value)
        self.app.width_text.set("320")
        self.app.height_text.set("240")
        self.app.keep_ratio.set(False)
        self.run_batch()
        for result in self.results().values():
            with Image.open(result.output) as out:
                self.assertEqual(out.size, (320, 240))

        self.app.width_text.set("500")
        self.app.height_text.set("")
        self.app.keep_ratio.set(True)
        self.run_batch()
        sizes = {name: (r.width, r.height) for name, r in self.results().items()}
        self.assertEqual(sizes, {"a.jpg": (500, 375), "b.png": (500, 375), "c photo.webp": (500, 375)})

        self.assertEqual(len([p for p in self.out.iterdir() if not p.name.startswith(".~")]), 12)
        self.assertEqual(self.messages, [])

    def test_size_mode_rejects_images_smaller_than_target(self):
        src = self.dir / "src"
        src.mkdir()
        photo(400, 300).save(src / "small.jpg", quality=95)
        photo(1000, 800).save(src / "large.jpg", quality=95)
        self.add_and_wait([src], expected=2)
        self.app.mode.set(Mode.SIZE.value)
        self.app.width_text.set("600")
        self.app.height_text.set("400")
        self.run_batch()
        self.assertEqual(self.status_of("small.jpg"), "失败：宽度超过原图（最大 400）")
        self.assertTrue(self.status_of("large.jpg").startswith("完成"))

    def test_cancel_finishes_cleanly(self):
        src = self.dir / "src"
        src.mkdir()
        for index in range(12):
            photo(900, 700, index).save(src / f"p{index}.jpg", quality=95)
        self.add_and_wait([src], expected=12)
        self.app.toggle_run()
        self.app.toggle_run()
        self.pump(lambda: not self.app.running)
        stats = self.app.run_stats
        self.assertEqual(stats["ok"] + stats["failed"] + stats["skipped"], 12)
        self.assertEqual(str(self.app.start_button.cget("text")), "开始压缩")


class InteractionTests(AppTestCase):
    def test_drop_accepts_tcl_path_lists(self):
        with mock.patch.object(self.app, "add_paths") as add_paths:
            self.app._on_drop(SimpleNamespace(data="{C:/My Photos/a b.jpg} C:/x.png"))
        add_paths.assert_called_once_with(["C:/My Photos/a b.jpg", "C:/x.png"])

    def test_size_mode_requires_dimensions(self):
        src = self.dir / "one.jpg"
        photo(100, 100).save(src)
        self.add_and_wait([src], expected=1)
        self.app.mode.set(Mode.SIZE.value)
        self.app.width_text.set("")
        self.app.height_text.set("50")
        self.app.toggle_run()
        self.assertFalse(self.app.running)
        self.assertEqual(self.messages[0][0], "showwarning")

    def test_empty_list_prompts_user(self):
        self.app.toggle_run()
        self.assertFalse(self.app.running)
        self.assertEqual(self.messages[0][0], "showinfo")

    def test_duplicates_remove_and_clear(self):
        src = self.make_folder()
        self.add_and_wait([src], expected=4)
        self.add_and_wait([src / "a.jpg"], expected=4)
        first = self.app.tree.get_children()[0]
        self.app.tree.selection_set(first)
        self.app.remove_selected()
        self.assertEqual(len(self.app.items), 3)
        self.app.clear()
        self.assertEqual(self.app.items, {})
        self.assertEqual(self.app.status.cget("text"), "尚未添加图片")

    def test_preferences_round_trip(self):
        self.app.mode.set(Mode.SCALE.value)
        self.app.scale_text.set("37")
        self.app.format_label.set("WebP")
        self.app._save_prefs()
        self.root.destroy()
        self.root = TkinterDnD.Tk()
        self.root.withdraw()
        restored = app_module.App(self.root)
        self.assertEqual((restored.mode.get(), restored.current_scale(), restored.format_label.get()),
                         (Mode.SCALE.value, 37, "WebP"))
        self.assertEqual(restored.output_dir.get(), str(self.out))


if __name__ == "__main__":
    unittest.main()
