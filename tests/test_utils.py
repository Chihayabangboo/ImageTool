# -*- coding: utf-8 -*-
"""纯逻辑测试：工具函数（不依赖 Pillow）。

覆盖：
- 图片格式判断（红线 4：只认 JPG/PNG）
- 路径处理与重名去重（红线 2：绝不覆盖）
- 质量值映射（红线 6：PNG 用 compress_level）
"""

import os
import sys

import pytest

# 兜底：直接用 python tests/test_utils.py 运行时也能找到项目模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import utils  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 图片格式判断
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "file_name",
    ["a.jpg", "a.jpeg", "a.JPG", "a.JPEG", "b.png", "b.PNG", "带中文的图.png"],
)
def test_supported_extensions_are_accepted(file_name):
    assert utils.is_supported_image(file_name) is True


@pytest.mark.parametrize(
    "file_name",
    ["a.txt", "a.psd", "a.raw", "a.heic", "a.svg", "a.mp4", "无扩展名", "a.jpg.txt", "", None],
)
def test_other_files_are_skipped(file_name):
    """非图片、未支持格式、无扩展名一律跳过（红线 4）。"""
    assert utils.is_supported_image(file_name) is False


@pytest.mark.parametrize("file_name", ["a.bmp", "a.webp", "a.tiff", "a.tif", "a.gif"])
def test_extended_formats_are_supported(file_name):
    """扩展支持的格式必须被识别（详见 tests/test_formats.py）。"""
    assert utils.is_supported_image(file_name) is True


def test_get_file_extension_normalizes_case():
    assert utils.get_file_extension("photo.JPG") == ".jpg"
    assert utils.get_file_extension("photo.JPEG") == ".jpeg"
    assert utils.get_file_extension("C:/tmp/图.PnG") == ".png"
    assert utils.get_file_extension("noext") == ""


def test_resolve_target_format_keeps_original_extension():
    """选择「原格式」时沿用原扩展名对应的格式。"""
    assert utils.resolve_target_format("a.jpg", utils.FORMAT_KEEP) == utils.FORMAT_JPEG
    assert utils.resolve_target_format("a.png", utils.FORMAT_KEEP) == utils.FORMAT_PNG
    assert utils.resolve_target_format("a.txt", utils.FORMAT_KEEP) is None


def test_resolve_target_format_respects_user_choice():
    assert utils.resolve_target_format("a.png", utils.FORMAT_JPEG) == utils.FORMAT_JPEG
    assert utils.resolve_target_format("a.jpg", utils.FORMAT_PNG) == utils.FORMAT_PNG


def test_format_extension_and_label():
    assert utils.get_format_extension(utils.FORMAT_JPEG) == ".jpg"
    assert utils.get_format_extension(utils.FORMAT_PNG) == ".png"
    assert utils.get_format_label(utils.FORMAT_JPEG) == "JPEG"
    assert utils.get_format_label(utils.FORMAT_KEEP) == "原格式"


# ---------------------------------------------------------------------------
# 2. 路径处理与重名去重
# ---------------------------------------------------------------------------
def test_build_output_path_uses_output_dir(tmp_path):
    output = utils.build_output_path(str(tmp_path), "C:/pics/photo.jpg", utils.FORMAT_JPEG)
    assert output == os.path.join(str(tmp_path), "photo.jpg")


def test_build_output_path_changes_extension_for_png(tmp_path):
    output = utils.build_output_path(str(tmp_path), "photo.jpg", utils.FORMAT_PNG)
    assert output == os.path.join(str(tmp_path), "photo.png")


def test_build_output_path_adds_suffix_when_exists(tmp_path):
    """输出文件已存在时自动加 (1)、(2) 后缀，绝不覆盖（红线 2）。"""
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"origin")  # 模拟已存在的原文件

    first = utils.build_output_path(str(tmp_path), "photo.jpg", utils.FORMAT_JPEG)
    assert os.path.basename(first) == "photo(1).jpg"

    (tmp_path / "photo(1).jpg").write_bytes(b"x")
    second = utils.build_output_path(str(tmp_path), "photo.jpg", utils.FORMAT_JPEG)
    assert os.path.basename(second) == "photo(2).jpg"

    # 原文件内容必须保持不变
    assert target.read_bytes() == b"origin"


def test_build_output_path_skips_existing_gaps(tmp_path):
    """序号不能只靠数量推算：删掉中间文件后仍不能覆盖已有文件。"""
    (tmp_path / "photo.jpg").write_bytes(b"x")
    (tmp_path / "photo(3).jpg").write_bytes(b"x")

    output = utils.build_output_path(str(tmp_path), "photo.jpg", utils.FORMAT_JPEG)
    assert os.path.basename(output) == "photo(4).jpg"


def test_build_output_path_keeps_name_with_suffix(tmp_path):
    """原文件名本身带括号后缀时也能正确处理。"""
    (tmp_path / "photo(1).jpg").write_bytes(b"x")
    output = utils.build_output_path(str(tmp_path), "photo(1).jpg", utils.FORMAT_JPEG)
    assert os.path.basename(output) == "photo(1)(1).jpg"


def test_file_name_helpers():
    assert utils.split_name_and_extension("photo.JPG") == ("photo", ".JPG")
    assert utils.add_suffix_to_file_name("photo.jpg", 2) == "photo(2).jpg"
    assert utils.split_suffix_from_file_name("photo(12).jpg") == ("photo", 12)
    assert utils.split_suffix_from_file_name("photo.jpg") == ("photo", None)


def test_list_image_files_filters_and_sorts(tmp_path):
    for name in ["b.png", "a.JPG", "c.txt", "notes.txt", "d.jpeg"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()  # 子文件夹必须被忽略（不递归）

    assert utils.list_image_files(str(tmp_path)) == ["a.JPG", "b.png", "d.jpeg"]
    assert utils.list_image_files(str(tmp_path / "not-exist")) == []


def test_list_image_files_returns_empty_on_read_error(tmp_path, monkeypatch):
    """读取目录失败时必须返回空列表，不能把异常抛给界面。"""

    def raise_denied(_folder):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(utils.os, "listdir", raise_denied)
    assert utils.list_image_files(str(tmp_path)) == []


def test_make_output_directory_creates_compressed_subfolder(tmp_path):
    output_dir = utils.make_output_directory(str(tmp_path))
    assert output_dir == os.path.join(str(tmp_path), utils.OUTPUT_SUBDIR_NAME)
    assert os.path.isdir(output_dir)
    # 重复调用必须幂等，不抛异常
    assert utils.make_output_directory(str(tmp_path)) == output_dir


def test_make_output_directory_raises_chinese_error(tmp_path, monkeypatch):
    """创建失败时抛出中文异常，供界面弹窗使用（红线 8）。"""

    def fake_makedirs(_path, **_kwargs):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(utils.os, "makedirs", fake_makedirs)

    with pytest.raises(utils.SubfolderCreationError) as exc_info:
        utils.make_output_directory(str(tmp_path))
    message = str(exc_info.value)
    assert "无法在所选文件夹创建子文件夹，请换一个文件夹" in message
    assert "Access is denied" in message  # 保留原因但不含英文堆栈


# ---------------------------------------------------------------------------
# 3. 质量值与保存参数映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "quality,expected",
    [(1, 0), (5, 0), (6, 1), (50, 5), (80, 7), (95, 9), (100, 9)],
)
def test_quality_to_compress_level_mapping(quality, expected):
    """1-100 映射到 0-9（红线 6）。"""
    assert utils.quality_to_compress_level(quality) == expected


@pytest.mark.parametrize(
    "value,expected", [(0, 1), (-5, 1), (101, 100), (1000, 100), ("abc", utils.QUALITY_DEFAULT), (None, 80)]
)
def test_clamp_quality_bounds(value, expected):
    assert utils.clamp_quality(value) == expected
    assert utils.QUALITY_MIN <= utils.clamp_quality(value) <= utils.QUALITY_MAX


def test_clamp_quality_rounds_float():
    assert utils.clamp_quality(79.6) == 80
    assert utils.clamp_quality("90") == 90


def test_default_quality_is_80():
    assert utils.QUALITY_DEFAULT == 80
    assert (utils.QUALITY_MIN, utils.QUALITY_MAX) == (1, 100)


def test_format_choices_use_chinese_labels():
    labels = [label for label, _value in utils.FORMAT_CHOICES]
    assert labels == ["原格式", "JPEG", "PNG"]
    assert utils.FORMAT_CHOICES[1][1] == utils.FORMAT_JPEG


# ---------------------------------------------------------------------------
# 4. 错误信息包装：全中文、单行、限长
# ---------------------------------------------------------------------------
def test_shorten_message_makes_single_line():
    text = utils.shorten_message("第一行\n第二行\r\n第三行")
    assert "\n" not in text and "\r" not in text
    assert text == "第一行 第二行 第三行"


def test_shorten_message_truncates_long_text():
    text = utils.shorten_message("长" * 500, limit=50)
    assert len(text) == 50
    assert text.endswith("...")


def test_format_error_message_is_chinese():
    message = utils.format_error_message("broken.jpg", ValueError("cannot identify image file"))
    assert message.startswith("处理失败：broken.jpg")
    assert "原因" in message
    assert "Traceback" not in message
