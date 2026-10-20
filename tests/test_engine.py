import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine  # noqa: E402
from engine import CompressError, Mode, OutputFormat, Settings  # noqa: E402


def photo(width, height, seed=0):
    """带渐变、噪点和色块的“照片”，让 JPEG 质量差异能体现在体积上。"""
    gradient = Image.linear_gradient("L")
    red = gradient.resize((width, height))
    green = gradient.rotate(90).resize((width, height))
    blue = Image.effect_noise((width, height), 40)
    image = Image.merge("RGB", (red, green, blue))
    draw = ImageDraw.Draw(image)
    rng = random.Random(seed)
    for _ in range(30):
        x, y = rng.randrange(width), rng.randrange(height)
        r = rng.randrange(5, max(6, min(width, height) // 4))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randrange(256) for _ in range(3)))
    return image


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.src = self.root / "src"
        self.out = self.root / "out"
        self.src.mkdir()

    def save(self, image, name, **options):
        path = self.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, **options)
        return path

    def compress(self, path, **settings):
        return engine.compress_image(path, Settings(**settings), self.out)


class QualityModeTests(EngineTestCase):
    def test_levels_keep_dimensions_and_lower_levels_are_smaller(self):
        source = self.save(photo(1600, 1200), "camera.jpg", quality=95)
        sizes = []
        for _, quality in engine.QUALITY_LEVELS:
            result = self.compress(source, mode=Mode.QUALITY, quality=quality)
            with Image.open(result.output) as out:
                self.assertEqual(out.size, (1600, 1200))
                self.assertEqual(out.format, "JPEG")
                self.assertNotIn(0x0112, out.getexif())
            self.assertFalse(result.kept_original)
            sizes.append(result.file_size)
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(len(set(sizes)), 4)
        self.assertLess(sizes[-1], source.stat().st_size)

    def test_exif_orientation_is_applied_and_removed(self):
        exif = Image.Exif()
        exif[0x0112] = 6
        source = self.save(photo(400, 300), "rotated.jpg", quality=95, exif=exif.tobytes())
        self.assertEqual((engine.probe(source).width, engine.probe(source).height), (300, 400))
        result = self.compress(source, mode=Mode.QUALITY, quality=65)
        with Image.open(result.output) as out:
            self.assertEqual(out.size, (300, 400))
            self.assertNotIn(0x0112, out.getexif())
        self.assertEqual((result.width, result.height), (300, 400))

    def test_png_is_optimized_losslessly(self):
        rgba = photo(500, 400).convert("RGBA")
        rgba.putalpha(Image.linear_gradient("L").resize((500, 400)))
        source = self.save(rgba, "alpha.png", compress_level=0)
        result = self.compress(source, mode=Mode.QUALITY, quality=30)
        with Image.open(result.output) as out:
            self.assertEqual(out.format, "PNG")
            self.assertEqual(out.convert("RGBA").tobytes(), rgba.tobytes())
        self.assertLess(result.file_size, source.stat().st_size)

    def test_opaque_rgba_png_is_stored_as_rgb_without_pixel_changes(self):
        rgba = photo(300, 200).convert("RGBA")
        source = self.save(rgba, "opaque.png", compress_level=1)
        result = self.compress(source, mode=Mode.QUALITY, quality=80)
        with Image.open(result.output) as out:
            self.assertEqual(out.mode, "RGB")
            self.assertEqual(out.tobytes(), rgba.convert("RGB").tobytes())

    def test_already_small_file_is_kept_instead_of_growing(self):
        source = self.save(photo(300, 200), "small.jpg", quality=30)
        result = self.compress(source, mode=Mode.QUALITY, quality=80)
        self.assertTrue(result.kept_original)
        self.assertEqual(result.output.read_bytes(), source.read_bytes())

    def test_jpeg_output_puts_transparency_on_white(self):
        rgba = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        rgba.putpixel((0, 0), (0, 0, 0, 0))
        ImageDraw.Draw(rgba).rectangle((0, 0, 20, 20), fill=(0, 0, 0, 0))
        source = self.save(rgba, "logo.png")
        result = self.compress(source, mode=Mode.QUALITY, quality=80, output_format=OutputFormat.JPEG)
        with Image.open(result.output) as out:
            self.assertEqual(out.mode, "RGB")
            self.assertTrue(all(channel > 240 for channel in out.getpixel((5, 5))))
            self.assertGreater(out.getpixel((50, 50))[0], 200)


class ScaleModeTests(EngineTestCase):
    def test_dimensions_are_floored_and_at_least_one_pixel(self):
        source = self.save(photo(1601, 1201), "big.jpg", quality=90)
        result = self.compress(source, mode=Mode.SCALE, scale_percent=50)
        self.assertEqual((result.width, result.height), (800, 600))
        with Image.open(result.output) as out:
            self.assertEqual(out.size, (800, 600))

        tiny = self.save(photo(50, 50), "tiny.png")
        result = self.compress(tiny, mode=Mode.SCALE, scale_percent=1)
        self.assertEqual((result.width, result.height), (1, 1))

    def test_full_scale_keeps_size(self):
        source = self.save(photo(320, 240), "same.jpg", quality=90)
        result = self.compress(source, mode=Mode.SCALE, scale_percent=100)
        self.assertEqual((result.width, result.height), (320, 240))

    def test_out_of_range_percent_is_rejected(self):
        source = self.save(photo(100, 100), "x.jpg")
        for percent in (0, 101):
            with self.assertRaises(CompressError):
                self.compress(source, mode=Mode.SCALE, scale_percent=percent)

    def test_palette_png_keeps_transparency_after_resize(self):
        image = Image.new("P", (100, 80), 1)
        image.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        ImageDraw.Draw(image).rectangle((0, 0, 49, 79), fill=0)
        source = self.save(image, "icon.png", transparency=0)
        result = self.compress(source, mode=Mode.SCALE, scale_percent=50)
        with Image.open(result.output) as out:
            self.assertEqual(out.size, (50, 40))
            rgba = out.convert("RGBA")
            self.assertEqual(rgba.getpixel((5, 20))[3], 0)
            self.assertEqual(rgba.getpixel((45, 20))[3], 255)


class SizeModeTests(EngineTestCase):
    def test_exact_size_stretches_to_requested_box(self):
        source = self.save(photo(400, 300), "s.jpg", quality=90)
        result = self.compress(source, mode=Mode.SIZE, width=300, height=100)
        with Image.open(result.output) as out:
            self.assertEqual(out.size, (300, 100))

    def test_exact_size_cannot_exceed_source(self):
        source = self.save(photo(400, 300), "s.jpg", quality=90)
        with self.assertRaisesRegex(CompressError, "宽度超过原图（最大 400）"):
            self.compress(source, mode=Mode.SIZE, width=401, height=100)
        with self.assertRaisesRegex(CompressError, "高度超过原图（最大 300）"):
            self.compress(source, mode=Mode.SIZE, width=100, height=301)
        with self.assertRaises(CompressError):
            self.compress(source, mode=Mode.SIZE, width=0, height=100)

    def test_keep_ratio_fits_inside_box_without_upscaling(self):
        cases = [
            ((1000, 800), (500, 0), (500, 400)),
            ((1000, 800), (500, 300), (375, 300)),
            ((1000, 800), (0, 200), (250, 200)),
            ((300, 200), (500, 0), (300, 200)),
        ]
        for source_size, box, expected in cases:
            with self.subTest(source=source_size, box=box):
                self.assertEqual(
                    engine.target_size(*source_size, Settings(mode=Mode.SIZE, width=box[0], height=box[1], keep_ratio=True)),
                    expected,
                )
        with self.assertRaises(CompressError):
            engine.target_size(100, 100, Settings(mode=Mode.SIZE, keep_ratio=True))


class FormatTests(EngineTestCase):
    def test_keep_format_rules(self):
        cases = [
            (photo(80, 60), "a.webp", {}, "WEBP", ".webp"),
            (photo(80, 60), "b.bmp", {}, "JPEG", ".jpg"),
            (photo(80, 60), "c.tif", {}, "JPEG", ".jpg"),
            (photo(80, 60), "d.jpeg", {"quality": 95}, "JPEG", ".jpeg"),
            (photo(80, 60), "e.png", {"format": "JPEG", "quality": 95}, "JPEG", ".jpg"),
        ]
        gif = Image.new("P", (40, 40), 0)
        gif.putpalette([0, 0, 0, 0, 255, 0] + [0] * 762)
        ImageDraw.Draw(gif).rectangle((10, 10, 30, 30), fill=1)
        cases.append((gif, "f.gif", {"transparency": 0}, "PNG", ".png"))

        for image, name, options, expected_format, expected_suffix in cases:
            with self.subTest(name=name):
                source = self.save(image, name, **options)
                result = self.compress(source, mode=Mode.SCALE, scale_percent=50)
                self.assertEqual(result.output.suffix, expected_suffix)
                with Image.open(result.output) as out:
                    self.assertEqual(out.format, expected_format)

    def test_explicit_output_formats(self):
        source = self.save(photo(120, 90), "pic.jpg", quality=90)
        for choice, expected in ((OutputFormat.PNG, "PNG"), (OutputFormat.WEBP, "WEBP"), (OutputFormat.JPEG, "JPEG")):
            with self.subTest(choice=choice):
                result = self.compress(source, mode=Mode.SCALE, scale_percent=50, output_format=choice)
                with Image.open(result.output) as out:
                    self.assertEqual(out.format, expected)
                    self.assertEqual(out.size, (60, 45))

    def test_cmyk_and_16bit_sources(self):
        cmyk = self.save(photo(100, 80).convert("CMYK"), "print.jpg", quality=90)
        result = self.compress(cmyk, mode=Mode.QUALITY, quality=50)
        with Image.open(result.output) as out:
            self.assertEqual(out.mode, "CMYK")
        result = self.compress(cmyk, mode=Mode.SCALE, scale_percent=50, output_format=OutputFormat.PNG)
        with Image.open(result.output) as out:
            self.assertEqual((out.mode, out.size), ("RGB", (50, 40)))

        deep = Image.linear_gradient("L").resize((64, 64)).convert("I").point(lambda v: v * 257).convert("I;16")
        deep_path = self.save(deep, "deep.png")
        result = self.compress(deep_path, mode=Mode.QUALITY, quality=80)
        with Image.open(result.output) as out:
            self.assertEqual(out.tobytes(), Image.open(deep_path).tobytes())
        result = self.compress(deep_path, mode=Mode.SCALE, scale_percent=50, output_format=OutputFormat.JPEG)
        with Image.open(result.output) as out:
            self.assertEqual((out.mode, out.size), ("L", (32, 32)))
            self.assertGreater(out.getextrema()[1], 200)


class FileHandlingTests(EngineTestCase):
    def test_name_conflicts_never_overwrite(self):
        first = self.save(photo(64, 64, 1), "a/x.jpg", quality=95)
        second = self.save(photo(64, 64, 2), "b/x.jpg", quality=95)
        out1 = self.compress(first, mode=Mode.SCALE, scale_percent=50).output
        out2 = self.compress(second, mode=Mode.SCALE, scale_percent=50).output
        self.assertEqual([out1.name, out2.name], ["x.jpg", "x (1).jpg"])

        original = first.read_bytes()
        in_place = engine.compress_image(first, Settings(mode=Mode.SCALE, scale_percent=50), first.parent)
        self.assertNotEqual(in_place.output, first)
        self.assertEqual(first.read_bytes(), original)

    def test_parallel_writes_get_unique_names(self):
        sources = [self.save(photo(64, 64, i), f"d{i}/same.jpg", quality=95) for i in range(8)]
        outputs, errors = [], []

        def work(path):
            try:
                outputs.append(self.compress(path, mode=Mode.SCALE, scale_percent=50).output)
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(p,)) for p in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({p.name for p in outputs}), 8)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(p.name for p in outputs))

    def test_collect_images(self):
        a = self.save(photo(10, 10), "a.jpg")
        b = self.save(photo(10, 10), "sub/b.png")
        self.save(photo(10, 10), "out/c.jpg")
        (self.src / "notes.txt").write_text("x")
        found = engine.collect_images([self.src, a], exclude_dir=self.src / "out")
        self.assertEqual(found, [a, b])
        explicit = engine.collect_images([self.src / "out"], exclude_dir=self.src / "out")
        self.assertEqual([p.name for p in explicit], ["c.jpg"])

    def test_corrupt_file(self):
        bad = self.src / "bad.jpg"
        bad.write_bytes(b"not really an image")
        with self.assertRaises(CompressError):
            engine.probe(bad)
        with self.assertRaises(CompressError):
            self.compress(bad, mode=Mode.QUALITY, quality=80)
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))


if __name__ == "__main__":
    unittest.main()
