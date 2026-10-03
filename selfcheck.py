# -*- coding: utf-8 -*-
"""一键自检脚本：python selfcheck.py

不需要 pytest，直接运行即可验证核心逻辑：
- 纯逻辑部分（格式判断、路径去重、参数映射、取消粒度、线程事件）总是运行
- 依赖 Pillow 的部分（透明图白底、EXIF 旋转、PNG 保存）在缺少依赖时自动跳过
- 任何一项失败都会打印中文错误信息，最后给出汇总
"""

import os
import sys
import threading

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _force_utf8_console():
    """把标准输出/错误切换成 UTF-8。

    Windows 中文控制台默认是 GBK，直接 print 中文可能抛 UnicodeEncodeError，
    这里做一次无害的重配置，失败也不影响自检本身。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()

import processor  # noqa: E402
import utils  # noqa: E402

RESULTS = []


def check(name, func):
    """执行一项检查并记录结果，单项失败不影响其它检查。"""
    try:
        func()
    except Exception as exc:  # noqa: BLE001 - 自检需要兜住所有异常继续跑
        RESULTS.append((name, False, utils.shorten_message(exc, 180)))
        print("[失败] {0} -> {1}".format(name, utils.shorten_message(exc, 180)))
    else:
        RESULTS.append((name, True, ""))
        print("[通过] {0}".format(name))


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# 纯逻辑检查
# ---------------------------------------------------------------------------
def check_supported_extension():
    for name in ["a.jpg", "b.JPEG", "c.png", "中文.PNG", "d.bmp", "e.webp", "f.tiff", "g.gif"]:
        assert_true(utils.is_supported_image(name), "{0} 应该被识别为支持的图片".format(name))
    for name in ["a.txt", "a.psd", "a.raw", "noext", ""]:
        assert_true(not utils.is_supported_image(name), "{0} 应该被跳过".format(name))


def check_quality_clamp():
    assert_true(utils.clamp_quality(-1) == 1, "质量下限应为 1")
    assert_true(utils.clamp_quality(999) == 100, "质量上限应为 100")
    assert_true(utils.clamp_quality("abc") == 80, "非法质量应回退到默认 80")


def check_png_options():
    for quality in range(1, 101):
        options = processor.build_save_options(utils.FORMAT_PNG, quality)
        assert_true("quality" not in options, "PNG 不能收到 quality 参数")
        assert_true(0 <= options["compress_level"] <= 9, "compress_level 必须在 0-9 之间")
    assert_true(
        processor.build_save_options(utils.FORMAT_JPEG, 80)["quality"] == 80,
        "JPEG 应使用 quality 参数",
    )


def check_output_path_conflict():
    """重名必须加 (1) 后缀，并且不覆盖已存在文件。"""
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        original = os.path.join(temp_dir, "photo.jpg")
        with open(original, "w", encoding="utf-8") as handle:
            handle.write("original")
        new_path = utils.build_output_path(temp_dir, original, utils.FORMAT_JPEG)
        assert_true(
            os.path.basename(new_path) == "photo(1).jpg",
            "重名时应生成 photo(1).jpg，实际：{0}".format(os.path.basename(new_path)),
        )
        with open(original, "r", encoding="utf-8") as handle:
            assert_true(handle.read() == "original", "原文件内容被改动了")


def check_transparency_detection():
    class FakeImage:
        def __init__(self, mode, info=None):
            self.mode = mode
            self.info = info or {}

    assert_true(processor.has_transparency(FakeImage("RGBA")), "RGBA 应被判定为透明图")
    assert_true(processor.has_transparency(FakeImage("LA")), "LA 应被判定为透明图")
    assert_true(
        processor.has_transparency(FakeImage("P", {"transparency": 0})),
        "带透明色的调色板图应被判定为透明图",
    )
    assert_true(not processor.has_transparency(FakeImage("RGB")), "RGB 不是透明图")


def check_chinese_error_message():
    message = processor.format_error_message("坏图.jpg", ValueError("cannot identify image file"))
    assert_true(message.startswith("处理失败："), "错误信息应以中文开头")
    assert_true("\n" not in message, "错误信息必须是单行")
    assert_true("Traceback" not in message, "不能把英文堆栈展示给用户")


def check_extension_table_consistency():
    """扩展名表、输出扩展名、显示名三者必须一致。"""
    for ext in utils.SUPPORTED_EXTENSIONS:
        assert_true(ext in utils.EXTENSION_TO_FORMAT, "{0} 缺少格式映射".format(ext))
    for fmt in utils.EXTENSION_TO_FORMAT.values():
        assert_true(utils.get_format_extension(fmt), "{0} 缺少输出扩展名".format(fmt))
    for ext in ["*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp", "*.tiff", "*.gif"]:
        assert_true(ext in utils.IMAGE_FILETYPE_PATTERN, "{0} 不在界面筛选器里".format(ext))


def check_multiframe_seek():
    """多帧图片必须 seek(0)，且每个文件只提示一次（用替身对象，不依赖真实文件）。"""
    import unittest.mock as mock

    fake = mock.MagicMock()
    fake.n_frames = 4
    logged = set()

    first = processor.seek_first_frame(fake, "loop.gif", notice_logged=logged)
    fake.seek.assert_called_once_with(0)
    assert_true("仅处理第一帧" in first, "多帧图片应给出中文提示，实际：{0}".format(first))

    second = processor.seek_first_frame(fake, "loop.gif", notice_logged=logged)
    assert_true(second == "", "同一个文件不应重复提示多帧信息")

    single = mock.MagicMock()
    single.n_frames = 1
    assert_true(
        processor.seek_first_frame(single, "photo.jpg", notice_logged=set()) == "",
        "单帧图片不应产生提示",
    )


def check_save_options_follow_final_format():
    """保存参数必须依据最终输出格式：JPEG/WebP 给 quality，PNG 给 compress_level。"""
    assert_true(
        processor.build_save_options(utils.FORMAT_JPEG, 80)["quality"] == 80,
        "JPEG 应使用 quality",
    )
    assert_true(
        processor.build_save_options(utils.FORMAT_WEBP, 70)["quality"] == 70,
        "WebP 应使用 quality",
    )
    png_options = processor.build_save_options(utils.FORMAT_PNG, 80)
    assert_true("quality" not in png_options, "PNG 不能收到 quality 参数")
    assert_true(png_options["compress_level"] == 7, "质量 80 应映射为 compress_level 7")
    gif_options = processor.build_save_options(utils.FORMAT_GIF, 80)
    assert_true("quality" not in gif_options, "GIF 不支持 quality，应忽略")


def check_cancel_granularity():
    """取消只在每张图片之间检查：3 张图处理完第 1 张后取消，只处理 1 张。"""
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        names = ["1.jpg", "2.jpg", "3.jpg"]
        for name in names:
            with open(os.path.join(temp_dir, name), "wb") as handle:
                handle.write(b"x")

        handled = []
        original_process = processor.process_one_image

        def fake_process(source_path, output_dir, selected_format, quality):
            handled.append(os.path.basename(source_path))
            return True, source_path, ""

        calls = {"count": 0}

        def cancel_check():
            calls["count"] += 1
            return calls["count"] > 1

        processor.process_one_image = fake_process
        try:
            batch = processor.BatchProcessor(
                files=names,
                input_dir=temp_dir,
                output_dir=temp_dir,
                selected_format=utils.FORMAT_KEEP,
                quality=80,
                cancel_check=cancel_check,
            )
            cancelled = False
            try:
                batch.run()
            except processor.BatchCancelledError:
                cancelled = True
        finally:
            processor.process_one_image = original_process

        assert_true(cancelled, "取消后应抛出 BatchCancelledError 并停止")
        assert_true(handled == ["1.jpg"], "取消后不应继续处理后续图片，实际：{0}".format(handled))


def check_worker_thread_events():
    """验证后台线程通过回调发事件，且不阻塞主线程。"""
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        with open(os.path.join(temp_dir, "a.jpg"), "wb") as handle:
            handle.write(b"x")

        events = []
        main_thread = threading.current_thread().name
        thread_names = []

        original_process = processor.process_one_image

        def fake_process(source_path, output_dir, selected_format, quality):
            thread_names.append(threading.current_thread().name)
            return True, source_path, ""

        processor.process_one_image = fake_process
        try:
            worker = processor.BatchWorker(
                input_path=temp_dir,
                selected_format=utils.FORMAT_KEEP,
                quality=80,
                emit=lambda event_type, payload: events.append((event_type, payload)),
            )
            thread = worker.start()
            assert_true(thread.daemon, "工作线程必须是守护线程")
            thread.join(timeout=20)
            assert_true(not thread.is_alive(), "工作线程应已结束")
        finally:
            processor.process_one_image = original_process

        event_types = [event_type for event_type, _payload in events]
        assert_true(processor.EVENT_FINISHED in event_types, "应发出 finished 事件")
        assert_true(processor.EVENT_FILE_DONE in event_types, "应发出 file_done 事件")
        assert_true(
            all(name != main_thread for name in thread_names),
            "图片处理必须发生在后台线程，不能占用主线程",
        )


# ---------------------------------------------------------------------------
# 依赖 Pillow 的检查
# ---------------------------------------------------------------------------
def check_with_pillow():
    """返回 True 表示 Pillow 可用并已执行检查。"""
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        print("[跳过] 依赖 Pillow 的检查：环境缺少 Pillow（{0}）".format(
            utils.shorten_message(exc, 120)
        ))
        print("       按要求不自动安装依赖，只运行了纯逻辑测试。")
        return False

    import tempfile

    def check_rgba_to_jpeg_white_background():
        source = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        source.paste((0, 255, 0, 255), (2, 2, 12, 12))
        result = processor.normalize_image_mode(source, utils.FORMAT_JPEG)
        assert_true(result.mode == "RGB", "转 JPEG 后应为 RGB 模式")
        assert_true(
            result.getpixel((0, 0)) == (255, 255, 255),
            "透明区域必须填成白色，实际：{0}".format(result.getpixel((0, 0))),
        )
        assert_true(
            result.getpixel((5, 5))[:3] == (0, 255, 0),
            "不透明区域颜色应保持不变",
        )

    def check_end_to_end_png_to_jpeg():
        """带透明通道的图选择 JPEG：工具应改为输出 PNG 并给出中文说明。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "透明图.png")
            image = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
            image.paste((255, 0, 0, 255), (4, 4, 14, 14))
            image.save(source_path)

            ok, result, note = processor.process_one_image(
                source_path, temp_dir, utils.FORMAT_JPEG, 90
            )
            assert_true(ok, "单张图片处理应成功：{0}".format(result))
            assert_true("透明通道" in note, "应给出中文说明，实际：{0}".format(note))
            with Image.open(result) as output_image:
                assert_true(output_image.format == "PNG", "带透明通道时应输出 PNG")
                assert_true(
                    output_image.getpixel((0, 0))[3] == 0,
                    "透明区域必须保留，不能被填成白底或黑色",
                )

    def check_duplicate_output_names():
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "photo.png")
            Image.new("RGB", (16, 16), (0, 0, 255)).save(source_path)
            ok1, path1, _n1 = processor.process_one_image(
                source_path, temp_dir, utils.FORMAT_JPEG, 90
            )
            ok2, path2, _n2 = processor.process_one_image(
                source_path, temp_dir, utils.FORMAT_JPEG, 90
            )
            assert_true(ok1 and ok2, "两次处理都应成功")
            assert_true(
                os.path.basename(path1) != os.path.basename(path2),
                "第二次输出必须改名而不是覆盖",
            )
            assert_true(os.path.isfile(path1) and os.path.isfile(path2), "两个输出文件都应存在")

    def check_broken_image_is_skipped():
        with tempfile.TemporaryDirectory() as temp_dir:
            Image.new("RGB", (16, 16), (0, 128, 0)).save(os.path.join(temp_dir, "good.jpg"))
            with open(os.path.join(temp_dir, "broken.jpg"), "wb") as handle:
                handle.write(b"not an image")
            ok, message, _note = processor.process_one_image(
                os.path.join(temp_dir, "broken.jpg"), temp_dir, utils.FORMAT_JPEG, 90
            )
            assert_true(not ok, "损坏图片应返回失败")
            assert_true("处理失败" in message, "失败信息应为中文提示")
            assert_true("Traceback" not in message, "不应向用户暴露英文堆栈")

    def check_exif_rotation():
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "rotated.jpg")
            image = Image.new("RGB", (40, 20), (255, 255, 255))
            exif = Image.Exif()
            exif[0x0112] = 6
            image.save(source_path, exif=exif)

            with Image.open(source_path) as raw:
                corrected = processor.correct_exif_orientation(raw)
                assert_true(
                    corrected.size == (20, 40),
                    "EXIF 旋转校正后尺寸应为 20x40，实际：{0}".format(corrected.size),
                )

    def check_png_compress_level():
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "photo.png")
            Image.new("RGB", (32, 32), (128, 128, 128)).save(source_path)
            ok, result, _note = processor.process_one_image(
                source_path, temp_dir, utils.FORMAT_PNG, 80
            )
            assert_true(ok, "PNG 保存应成功（不应因为 quality 参数报错）：{0}".format(result))
            with Image.open(result) as output_image:
                assert_true(output_image.format == "PNG", "输出应为 PNG")

    def check_batch_with_broken_file():
        with tempfile.TemporaryDirectory() as temp_dir:
            Image.new("RGB", (16, 16), (1, 2, 3)).save(os.path.join(temp_dir, "a.jpg"))
            with open(os.path.join(temp_dir, "bad.jpg"), "wb") as handle:
                handle.write(b"broken")
            Image.new("RGB", (16, 16), (4, 5, 6)).save(os.path.join(temp_dir, "b.png"))

            events = []
            worker = processor.BatchWorker(
                input_path=temp_dir,
                selected_format=utils.FORMAT_KEEP,
                quality=80,
                emit=lambda event_type, payload: events.append((event_type, payload)),
            )
            worker.start().join(timeout=30)

            finished = [
                payload for event_type, payload in events
                if event_type == processor.EVENT_FINISHED
            ]
            assert_true(bool(finished), "应收到 finished 事件")
            summary = finished[-1]
            assert_true(summary["total"] == 3, "应处理 3 张图片")
            assert_true(summary["success"] == 2, "应有 2 张成功")
            assert_true(summary["failed"] == 1, "应有 1 张失败")
            output_dir = os.path.join(temp_dir, utils.OUTPUT_SUBDIR_NAME)
            assert_true(
                os.path.isfile(os.path.join(output_dir, "b.png")),
                "损坏图片之后的图片也应处理完成",
            )

    check("透明图转 JPEG 铺白底（红线 1）", check_rgba_to_jpeg_white_background)
    check("透明 PNG 端到端转 JPEG", check_end_to_end_png_to_jpeg)
    check("重名自动加后缀不覆盖（红线 2）", check_duplicate_output_names)
    check("损坏图片只记录不中断（红线 4）", check_broken_image_is_skipped)
    check("EXIF 旋转校正（红线 7）", check_exif_rotation)
    check("PNG 使用 compress_level（红线 6）", check_png_compress_level)
    check("批量任务含损坏图片", check_batch_with_broken_file)
    return True


def main():
    print("=" * 60)
    print("ImageTool 自检开始（Python {0}）".format(sys.version.split()[0]))
    print("=" * 60)

    check("图片格式判断（新旧格式）", check_supported_extension)
    check("扩展名表与界面筛选器一致性", check_extension_table_consistency)
    check("质量值范围限制", check_quality_clamp)
    check("PNG/JPEG/WebP/GIF 保存参数映射", check_save_options_follow_final_format)
    check("多帧图片 seek(0) 与只提示一次", check_multiframe_seek)
    check("重名路径生成逻辑", check_output_path_conflict)
    check("透明通道判断", check_transparency_detection)
    check("中文错误信息包装", check_chinese_error_message)
    check("取消粒度（每张之间检查）", check_cancel_granularity)
    check("后台线程事件流", check_worker_thread_events)

    pillow_used = check_with_pillow()

    passed = sum(1 for _name, ok, _msg in RESULTS if ok)
    failed = sum(1 for _name, ok, _msg in RESULTS if not ok)

    print("-" * 60)
    print("通过 {0} 项，失败 {1} 项。".format(passed, failed))
    if not pillow_used:
        print("注意：环境缺少 Pillow，依赖图片库的检查已被跳过（未执行任何安装命令）。")
    if failed:
        print("失败明细：")
        for name, ok, message in RESULTS:
            if not ok:
                print("  - {0}：{1}".format(name, message))
        return 1
    print("自检全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
