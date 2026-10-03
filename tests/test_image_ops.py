# -*- coding: utf-8 -*-
"""依赖 Pillow 的测试（红线 9：导入放在 try/except 里，缺失时优雅跳过）。

如果当前环境没有安装 Pillow，本文件所有用例会被 pytest 标记为 skipped，
绝不会因为 ImportError 直接中断整个测试过程。

覆盖：
- 透明图转 JPEG 必须铺白底（红线 1）
- 重名自动加 (1)、(2) 后缀且不覆盖原文件（红线 2）
- 单张损坏图片不影响其它图片（红线 4）
- PNG 输出使用 compress_level（红线 6）
- EXIF 旋转校正（红线 7）
- 输出子文件夹创建失败（红线 8）
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import utils  # noqa: E402

# 把 Pillow 的导入包在 try/except 里，保证环境缺少 Pillow 时能优雅跳过
try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageDraw

    PIL_AVAILABLE = True
    PIL_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - 任何导入异常都视为环境不满足
    PIL_AVAILABLE = False
    PIL_IMPORT_ERROR = str(exc)
    Image = None
    ImageDraw = None

import processor  # noqa: E402

pytestmark = pytest.mark.skipif(
    not PIL_AVAILABLE,
    reason="环境缺少 Pillow，跳过依赖图片库的测试（未安装依赖，也不自动安装）：{0}".format(
        PIL_IMPORT_ERROR
    ),
)


def make_rgba_image(size=(20, 20), box=(2, 2, 12, 12), color=(0, 255, 0, 255)):
    """生成一张透明背景 + 中间不透明色块的 RGBA 图。"""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste(color, box)
    return image


def collect_events(tmp_path, selected_format=utils.FORMAT_KEEP, quality=80):
    """启动后台任务并收集事件（替代界面里的 queue.Queue）。"""
    events = []
    worker = processor.BatchWorker(
        input_path=str(tmp_path),
        selected_format=selected_format,
        quality=quality,
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )
    worker.start().join(timeout=30)
    return events


def get_event(events, event_type):
    matched = [payload for event, payload in events if event == event_type]
    assert matched, "没有收到 {0} 事件，实际事件：{1}".format(
        event_type, [event for event, _payload in events]
    )
    return matched[-1]


# ---------------------------------------------------------------------------
# 红线 1：透明图转 JPEG 必须铺白底，不能直接 convert('RGB')
# ---------------------------------------------------------------------------
def test_rgba_to_jpeg_pastes_on_white_background():
    source = make_rgba_image()
    result = processor.normalize_image_mode(source, utils.FORMAT_JPEG)

    assert result.mode == "RGB"
    # 原来的透明角落在输出里必须是白色，而不是黑色
    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((19, 19)) == (255, 255, 255)
    # 不透明色块保持原色并覆盖在白色背景之上
    assert result.getpixel((5, 5))[:3] == (0, 255, 0)


def test_rgba_to_jpeg_end_to_end_keeps_transparency(tmp_path):
    """带透明通道的图选择 JPEG 时，工具改为输出 PNG 以保留透明区域（并给出提示）。"""
    source_path = tmp_path / "transparent.png"
    make_rgba_image().save(source_path)

    ok, output_path, note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 95
    )
    assert ok, output_path
    assert "透明通道" in note, "应告知用户为什么没有按选择转成 JPEG"

    with Image.open(output_path) as output_image:
        assert output_image.format == "PNG"
        assert output_image.mode == "RGBA"
        assert output_image.getpixel((0, 0))[3] == 0, "透明区域必须保留"


def test_la_mode_transparency_to_jpeg(tmp_path):
    grey_with_alpha = Image.new("LA", (10, 10), (128, 0))
    result = processor.normalize_image_mode(grey_with_alpha, utils.FORMAT_JPEG)
    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == (255, 255, 255)


def test_palette_transparency_to_jpeg(tmp_path):
    palette_image = Image.new("P", (10, 10), 0)
    palette_image.info["transparency"] = 0
    result = processor.normalize_image_mode(palette_image, utils.FORMAT_JPEG)
    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == (255, 255, 255)


def test_rgb_image_is_not_copied_again():
    """已经是 RGB 的图不需要重新构造，避免无意义的内存拷贝。"""
    rgb_image = Image.new("RGB", (8, 8), (10, 20, 30))
    assert processor.normalize_image_mode(rgb_image, utils.FORMAT_JPEG) is rgb_image


def test_png_output_keeps_transparency(tmp_path):
    """输出 PNG 时应保留透明通道，不能强行转成 RGB。"""
    source_path = tmp_path / "transparent.png"
    make_rgba_image().save(source_path)

    ok, output_path, _note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_PNG, 80
    )
    assert ok, output_path
    with Image.open(output_path) as output_image:
        assert output_image.mode == "RGBA"
        assert output_image.getpixel((0, 0))[3] == 0


# ---------------------------------------------------------------------------
# 红线 2：重名自动加后缀，绝不覆盖
# ---------------------------------------------------------------------------
def test_existing_output_is_never_overwritten(tmp_path):
    source_path = tmp_path / "photo.png"
    Image.new("RGB", (16, 16), (200, 30, 30)).save(source_path)

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"ORIGINAL-USER-FILE")

    ok, output_path, _note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 80
    )
    assert ok, output_path

    # 原文件保持字节不变
    assert existing.read_bytes() == b"ORIGINAL-USER-FILE"
    assert os.path.basename(output_path) == "photo(1).jpg"
    assert os.path.isfile(output_path)


def test_second_run_creates_next_suffix(tmp_path):
    source_path = tmp_path / "photo.png"
    Image.new("RGB", (16, 16), (30, 30, 200)).save(source_path)

    first_ok, first_path, _n1 = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 80
    )
    second_ok, second_path, _n2 = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 80
    )
    third_ok, third_path, _n3 = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 80
    )

    assert first_ok and second_ok and third_ok
    names = sorted(
        [os.path.basename(first_path), os.path.basename(second_path), os.path.basename(third_path)]
    )
    assert names == ["photo(1).jpg", "photo(2).jpg", "photo.jpg"]


# ---------------------------------------------------------------------------
# 红线 4：损坏图片只记录日志，不中断队列
# ---------------------------------------------------------------------------
def test_broken_image_does_not_break_the_batch(tmp_path):
    Image.new("RGB", (16, 16), (0, 128, 0)).save(tmp_path / "good1.jpg")
    (tmp_path / "broken.jpg").write_bytes(b"this-is-not-an-image-at-all")
    Image.new("RGB", (16, 16), (0, 0, 128)).save(tmp_path / "good2.png")

    events = collect_events(tmp_path)
    finished = get_event(events, processor.EVENT_FINISHED)

    assert finished["total"] == 3
    assert finished["success"] == 2
    assert finished["failed"] == 1
    assert "broken.jpg" in finished["failures"][0]
    assert "处理失败" in finished["failures"][0]
    # 损坏图片之后的图片照样处理成功（默认「原格式」：PNG 仍输出为 PNG）
    assert os.path.isfile(tmp_path / utils.OUTPUT_SUBDIR_NAME / "good2.png")


def test_gif_is_processed_like_any_supported_format(tmp_path):
    """GIF 现在是受支持格式：按「原格式」输出为同名 .gif，处理成功。"""
    Image.new("RGB", (10, 10), (1, 1, 1)).save(tmp_path / "animation.gif")

    events = collect_events(tmp_path)
    finished = get_event(events, processor.EVENT_FINISHED)

    assert finished["total"] == 1
    assert finished["success"] == 1
    assert finished["failed"] == 0
    assert os.path.isfile(tmp_path / utils.OUTPUT_SUBDIR_NAME / "animation.gif")


def test_gif_can_be_converted_to_jpeg(tmp_path):
    """GIF（P 调色板模式）转 JPEG 必须成功：需要先做模式归一化。"""
    Image.new("RGB", (10, 10), (1, 1, 1)).save(tmp_path / "animation.gif")

    events = collect_events(tmp_path, selected_format=utils.FORMAT_JPEG)
    finished = get_event(events, processor.EVENT_FINISHED)

    assert finished["success"] == 1
    assert finished["failed"] == 0
    output = tmp_path / utils.OUTPUT_SUBDIR_NAME / "animation.jpg"
    assert os.path.isfile(output)
    with Image.open(output) as output_image:
        assert output_image.format == "JPEG"


# ---------------------------------------------------------------------------
# 红线 6：PNG 参数映射真的能保存成功
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("quality", [1, 50, 80, 100])
def test_png_save_with_mapped_compress_level(tmp_path, quality):
    source_path = tmp_path / "photo.png"
    Image.new("RGB", (32, 32), (120, 120, 120)).save(source_path)

    ok, output_path, _note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_PNG, quality
    )
    assert ok, output_path
    with Image.open(output_path) as output_image:
        assert output_image.format == "PNG"
        assert output_image.size == (32, 32)


@pytest.mark.parametrize("quality", [1, 50, 80, 100])
def test_jpeg_save_with_quality(tmp_path, quality):
    source_path = tmp_path / "photo.png"
    Image.new("RGB", (32, 32), (120, 60, 30)).save(source_path)

    ok, output_path, _note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, quality
    )
    assert ok, output_path
    with Image.open(output_path) as output_image:
        assert output_image.format == "JPEG"


# ---------------------------------------------------------------------------
# 红线 7：EXIF 旋转校正
# ---------------------------------------------------------------------------
def make_exif_rotated_jpeg(path, size=(40, 20)):
    """生成一张带 EXIF Orientation=6（需顺时针旋转 90 度）的横图。"""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 左上角画红块，便于验证旋转方向
    draw.rectangle([0, 0, 9, 9], fill=(255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(path, exif=exif)
    return path


def test_exif_transpose_rotates_image(tmp_path):
    source_path = make_exif_rotated_jpeg(tmp_path / "rotated.jpg")

    with Image.open(source_path) as raw:
        assert raw.size == (40, 20)
        corrected = processor.correct_exif_orientation(raw)
        # Orientation=6 表示需要旋转 90 度，尺寸应变成 20x40（竖图）
        assert corrected.size == (20, 40)


def test_exif_orientation_is_applied_end_to_end(tmp_path):
    source_path = make_exif_rotated_jpeg(tmp_path / "rotated.jpg")

    ok, output_path, _note = processor.process_one_image(
        str(source_path), str(tmp_path), utils.FORMAT_JPEG, 95
    )
    assert ok, output_path
    with Image.open(output_path) as output_image:
        assert output_image.size == (20, 40), "输出图片没有按 EXIF 校正方向"


def test_exif_transpose_falls_back_when_pillow_lacks_feature(monkeypatch):
    """老版本 Pillow 没有 exif_transpose 时，必须原样返回而不是报错。"""
    import PIL.ImageOps as ImageOps

    monkeypatch.delattr(ImageOps, "exif_transpose", raising=False)
    image = Image.new("RGB", (5, 7), (1, 2, 3))
    assert processor.correct_exif_orientation(image) is image


# ---------------------------------------------------------------------------
# 红线 8：输入文件夹无法写入
# ---------------------------------------------------------------------------
def test_batch_reports_folder_error_and_stops(tmp_path, monkeypatch):
    Image.new("RGB", (16, 16), (9, 9, 9)).save(tmp_path / "a.jpg")

    def denied(_parent_dir):
        raise utils.SubfolderCreationError("无法在所选文件夹创建子文件夹，请换一个文件夹")

    monkeypatch.setattr(processor, "make_output_directory", denied)

    events = collect_events(tmp_path)
    event_types = [event_type for event_type, _payload in events]

    assert processor.EVENT_FOLDER_ERROR in event_types
    assert processor.EVENT_FINISHED not in event_types
    message = get_event(events, processor.EVENT_FOLDER_ERROR)["message"]
    assert "无法在所选文件夹创建子文件夹" in message


# ---------------------------------------------------------------------------
# 完整链路：批量处理 + 输出目录
# ---------------------------------------------------------------------------
def test_full_batch_writes_into_compressed_folder(tmp_path):
    Image.new("RGB", (24, 24), (10, 200, 10)).save(tmp_path / "one.jpg", quality=90)
    make_rgba_image().save(tmp_path / "two.png")
    (tmp_path / "ignore.txt").write_text("skip me", encoding="utf-8")

    events = collect_events(tmp_path, selected_format=utils.FORMAT_JPEG, quality=85)
    finished = get_event(events, processor.EVENT_FINISHED)
    output_dir = tmp_path / utils.OUTPUT_SUBDIR_NAME

    assert finished["total"] == 2
    assert finished["success"] == 2
    assert finished["failed"] == 0
    assert os.path.isfile(output_dir / "one.jpg")
    # 第二张是带透明通道的图：选择 JPEG 也会保留为 PNG，避免丢失透明区域
    assert os.path.isfile(output_dir / "two.png")
    assert not (output_dir / "ignore.txt").exists()


def test_test_image_data_is_valid(tmp_path):
    """自检：确保测试用的图片数据本身可用（否则上面的断言没有意义）。"""
    path = tmp_path / "sanity.png"
    make_rgba_image().save(path)
    with Image.open(path) as image:
        assert image.size == (20, 20)
        assert image.mode == "RGBA"
