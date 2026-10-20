"""批量图片压缩引擎：无损压缩、等比缩放、指定尺寸。"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 1_000_000_000

SUPPORTED_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".dib", ".gif", ".tif", ".tiff"}
)

QUALITY_LEVELS: tuple[tuple[str, int], ...] = (("低", 30), ("中", 50), ("高", 65), ("超高", 80))
RESIZE_QUALITY = 80

_JPEG_EXTENSIONS = (".jpg", ".jpeg", ".jpe", ".jfif")
_FAMILY = {"JPEG": "JPEG", "MPO": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}
_DEFAULT_EXTENSION = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_SWAPPED_ORIENTATIONS = frozenset({5, 6, 7, 8})
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
_COLOR_SPACES_WITHOUT_ICC = frozenset({"CMYK", "YCbCr", "LAB", "HSV"})


class Mode(str, Enum):
    QUALITY = "quality"
    SCALE = "scale"
    SIZE = "size"


class OutputFormat(str, Enum):
    KEEP = "keep"
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.QUALITY
    quality: int = 80
    scale_percent: int = 100
    width: int = 0
    height: int = 0
    keep_ratio: bool = False
    output_format: OutputFormat = OutputFormat.KEEP


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    file_size: int
    format: str


@dataclass(frozen=True)
class Result:
    source: Path
    output: Path
    width: int
    height: int
    file_size: int
    kept_original: bool


class CompressError(Exception):
    """可以直接展示给用户的失败原因。"""


def is_supported(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def path_key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(path))


def collect_images(
    paths: Iterable[str | os.PathLike], exclude_dir: str | os.PathLike | None = None
) -> list[Path]:
    """展开文件夹（含子文件夹），去重并过滤出支持的图片。

    exclude_dir 只在作为子文件夹出现时跳过，以免把上次的输出结果再次加入。
    """
    excluded = path_key(exclude_dir) if exclude_dir else None
    seen: set[str] = set()
    found: list[Path] = []

    def add(candidate: Path) -> None:
        key = path_key(candidate)
        if key not in seen and is_supported(candidate):
            seen.add(key)
            found.append(candidate)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            top = path_key(path)
            for root, dirs, files in os.walk(path):
                key = path_key(root)
                if excluded and key == excluded and key != top:
                    dirs[:] = []
                    continue
                dirs.sort()
                for name in sorted(files):
                    add(Path(root) / name)
        elif path.is_file():
            add(path)
    return found


def probe(path: str | os.PathLike) -> ImageInfo:
    """只读取文件头，返回按 EXIF 方向校正后的宽高。"""
    path = Path(path)
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
            if _orientation(image) in _SWAPPED_ORIENTATIONS:
                width, height = height, width
        size = path.stat().st_size
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise CompressError("不是可识别的图片文件") from exc
    except OSError as exc:
        raise CompressError(f"无法读取：{exc.strerror or exc}") from exc
    return ImageInfo(path, width, height, size, image_format)


def target_size(width: int, height: int, settings: Settings) -> tuple[int, int]:
    if settings.mode is Mode.QUALITY:
        return width, height

    if settings.mode is Mode.SCALE:
        percent = settings.scale_percent
        if not 1 <= percent <= 100:
            raise CompressError("缩放比例应为 1%~100%")
        return max(1, width * percent // 100), max(1, height * percent // 100)

    if settings.keep_ratio:
        if settings.width <= 0 and settings.height <= 0:
            raise CompressError("请至少填写宽度或高度")
        ratio = min(
            settings.width / width if settings.width > 0 else 1.0,
            settings.height / height if settings.height > 0 else 1.0,
            1.0,
        )
        return max(1, round(width * ratio)), max(1, round(height * ratio))

    if settings.width < 1 or settings.height < 1:
        raise CompressError("请填写宽度和高度")
    if settings.width > width:
        raise CompressError(f"宽度超过原图（最大 {width}）")
    if settings.height > height:
        raise CompressError(f"高度超过原图（最大 {height}）")
    return settings.width, settings.height


def output_format_for(
    source_format: str, source_suffix: str, has_alpha: bool, choice: OutputFormat
) -> tuple[str, str]:
    """返回 (Pillow 格式名, 输出扩展名)。

    保持原格式时，BMP/GIF/TIFF 这类无法按质量压缩的格式改存为 JPG，带透明度的改存为 PNG。
    """
    if choice is OutputFormat.JPEG:
        return "JPEG", ".jpg"
    if choice is OutputFormat.PNG:
        return "PNG", ".png"
    if choice is OutputFormat.WEBP:
        return "WEBP", ".webp"

    family = _FAMILY.get(source_format)
    if family is None:
        return ("PNG", ".png") if has_alpha else ("JPEG", ".jpg")
    suffix = source_suffix.lower()
    if family == "JPEG" and suffix in _JPEG_EXTENSIONS:
        return family, suffix
    return family, _DEFAULT_EXTENSION[family]


def compress_image(
    source: str | os.PathLike, settings: Settings, output_dir: str | os.PathLike
) -> Result:
    source = Path(source)
    output_dir = Path(output_dir)
    try:
        source_size = source.stat().st_size
        with Image.open(source) as opened:
            source_format = (opened.format or "").upper()
            icc_profile = opened.info.get("icc_profile")
            image = _upright(opened)
        image.info = {k: v for k, v in image.info.items() if k == "transparency"}

        width, height = image.size
        new_width, new_height = target_size(width, height, settings)
        if (new_width, new_height) != (width, height):
            image, icc_profile = _prepare_for_resize(image, icc_profile)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS, reducing_gap=3.0)

        pil_format, suffix = output_format_for(
            source_format, source.suffix, _has_alpha(image), settings.output_format
        )
        quality = settings.quality if settings.mode is Mode.QUALITY else RESIZE_QUALITY
        data = _encode(image, pil_format, quality, icc_profile)

        kept_original = False
        if (
            settings.mode is Mode.QUALITY
            and _FAMILY.get(source_format) == pil_format
            and len(data) >= source_size
        ):
            data = source.read_bytes()
            kept_original = True

        output_dir.mkdir(parents=True, exist_ok=True)
        destination = _write_unique(output_dir, source, suffix, data)
    except CompressError:
        raise
    except UnidentifiedImageError as exc:
        raise CompressError("不是可识别的图片文件") from exc
    except Image.DecompressionBombError as exc:
        raise CompressError("图片像素过大，无法处理") from exc
    except MemoryError as exc:
        raise CompressError("内存不足，图片过大") from exc
    except OSError as exc:
        raise CompressError(f"读写失败：{exc.strerror or exc}") from exc

    return Result(source, destination, new_width, new_height, len(data), kept_original)


def _orientation(image: Image.Image) -> int:
    try:
        return int(image.getexif().get(0x0112, 1))
    except Exception:  # 损坏的 EXIF 不应妨碍读取图片本身
        return 1


def _upright(image: Image.Image) -> Image.Image:
    try:
        return ImageOps.exif_transpose(image)
    except Exception:  # 同上：EXIF 损坏时按原方向处理
        return image.copy()


def _has_alpha(image: Image.Image) -> bool:
    if image.mode in _ALPHA_MODES:
        return True
    return image.mode in ("P", "L", "RGB", "1") and "transparency" in image.info


def _is_fully_opaque(image: Image.Image) -> bool:
    return image.getchannel("A").getextrema() == (255, 255)


def _is_high_bit_depth(image: Image.Image) -> bool:
    return image.mode.startswith("I") or image.mode == "F"


def _to_8bit_gray(image: Image.Image) -> Image.Image:
    if image.mode.startswith("I;16"):
        image = image.convert("I")
    high = image.getextrema()[1]
    if high <= 255:
        return image.convert("L")
    return image.point(lambda value: value * (255 / high)).convert("L")


def _prepare_for_resize(image: Image.Image, icc_profile: bytes | None):
    """把调色板、1 位、16 位等模式转换成可做高质量缩放的模式。"""
    mode = image.mode
    if mode == "1":
        return image.convert("L"), icc_profile
    if mode in ("P", "PA"):
        return image.convert("RGBA" if _has_alpha(image) else "RGB"), icc_profile
    if mode in ("L", "RGB") and "transparency" in image.info:
        return image.convert("LA" if mode == "L" else "RGBA"), icc_profile
    if _is_high_bit_depth(image):
        return _to_8bit_gray(image), icc_profile
    if mode in ("RGBa", "La"):
        return image.convert(mode.upper()), icc_profile
    if mode in ("YCbCr", "LAB", "HSV"):
        return image.convert("RGB"), None
    return image, icc_profile


def _flatten(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _encode(image: Image.Image, pil_format: str, quality: int, icc_profile: bytes | None) -> bytes:
    if pil_format == "JPEG":
        if _has_alpha(image):
            grayscale = image.mode in ("L", "LA", "La")
            image = _flatten(image)
            if grayscale:
                image = image.convert("L")
        elif image.mode == "1":
            image = image.convert("L")
        elif _is_high_bit_depth(image):
            image = _to_8bit_gray(image)
        elif image.mode not in ("L", "RGB", "CMYK"):
            image = image.convert("RGB")
            icc_profile = None
        options = {"quality": quality, "optimize": True, "progressive": True}

    elif pil_format == "PNG":
        if image.mode in ("RGBA", "LA") and _is_fully_opaque(image):
            image = image.convert("RGB" if image.mode == "RGBA" else "L")
        elif image.mode in _COLOR_SPACES_WITHOUT_ICC:
            image = image.convert("RGB")
            icc_profile = None
        elif image.mode in ("RGBa", "La", "PA"):
            image = image.convert("RGBA")
        options = {"optimize": True}

    else:
        if image.mode in _COLOR_SPACES_WITHOUT_ICC:
            image = image.convert("RGB")
            icc_profile = None
        elif _is_high_bit_depth(image):
            image = _to_8bit_gray(image).convert("RGB")
        elif image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if _has_alpha(image) else "RGB")
        if image.mode == "RGBA" and _is_fully_opaque(image):
            image = image.convert("RGB")
        options = {"quality": quality, "method": 6}

    if icc_profile:
        options["icc_profile"] = icc_profile

    buffer = io.BytesIO()
    image.save(buffer, pil_format, **options)
    return buffer.getvalue()


def _write_unique(directory: Path, source: Path, suffix: str, data: bytes) -> Path:
    """在目录中写入一个新文件，绝不覆盖已有文件或原图。

    先用 O_EXCL 占住文件名（多线程同名时不冲突），再写临时文件并替换，避免留下半截文件。
    """
    source_key = path_key(source)
    for index in range(10_000):
        name = f"{source.stem}{suffix}" if index == 0 else f"{source.stem} ({index}){suffix}"
        candidate = directory / name
        if path_key(candidate) == source_key:
            continue
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        os.close(handle)

        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".~", suffix=suffix)
            with os.fdopen(fd, "wb") as temp_file:
                temp_file.write(data)
            os.replace(temp_name, candidate)
        except BaseException:
            for leftover in (temp_name, candidate):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            raise
        return candidate
    raise CompressError("同名文件过多，请清理输出文件夹")
