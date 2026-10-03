# -*- coding: utf-8 -*-
"""纯逻辑测试：processor 的逻辑分支 + 后台线程事件流（不依赖 Pillow）。

重点验证：
- 保存参数映射（红线 6：PNG 用 compress_level，不传 quality）
- 透明通道判断（红线 1 的判断依据）
- 中文错误信息（红线 5）
- 取消粒度（红线 3：每张之间检查，不中断当前图片）
- 子线程只通过回调发事件，不碰 Tkinter（红线 3）
- 子文件夹创建失败的事件（红线 8）
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import processor  # noqa: E402
import utils  # noqa: E402


class FakeImage:
    """假的 Pillow Image 对象，只带 mode / info / size，用于纯逻辑判断。"""

    def __init__(self, mode="RGB", info=None, size=(4, 4)):
        self.mode = mode
        self.info = {} if info is None else dict(info)
        self.size = size


# ---------------------------------------------------------------------------
# 1. 保存参数映射
# ---------------------------------------------------------------------------
def test_jpeg_save_options_use_quality():
    options = processor.build_save_options(utils.FORMAT_JPEG, 80)
    assert options["quality"] == 80
    assert "compress_level" not in options


def test_png_save_options_use_compress_level_not_quality():
    """红线 6：PNG 必须用 compress_level，绝不能把 quality 传给 PNG。"""
    for quality in range(1, 101):
        options = processor.build_save_options(utils.FORMAT_PNG, quality)
        assert "quality" not in options, "PNG 不能收到 quality 参数"
        assert "format" not in options
        assert 0 <= options["compress_level"] <= 9
    assert processor.build_save_options(utils.FORMAT_PNG, 80)["compress_level"] == 7
    assert processor.build_save_options(utils.FORMAT_PNG, 100)["compress_level"] == 9


def test_unknown_format_returns_empty_options():
    assert processor.build_save_options("BMP", 80) == {}


def test_save_options_clamp_out_of_range_quality():
    assert processor.build_save_options(utils.FORMAT_JPEG, 999)["quality"] == 100
    assert processor.build_save_options(utils.FORMAT_JPEG, 0)["quality"] == 1


# ---------------------------------------------------------------------------
# 2. 透明通道判断（红线 1 的判断依据）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["RGBA", "LA", "PA", "RGBa", "CMYKA"])
def test_modes_with_alpha_are_transparent(mode):
    assert processor.has_transparency(FakeImage(mode=mode)) is True


def test_palette_with_transparency_is_transparent():
    assert processor.has_transparency(FakeImage(mode="P", info={"transparency": 0})) is True
    assert processor.has_transparency(FakeImage(mode="P", info={})) is False


@pytest.mark.parametrize("mode", ["RGB", "L", "CMYK", "P", "I;16"])
def test_modes_without_alpha_are_opaque(mode):
    assert processor.has_transparency(FakeImage(mode=mode)) is False


def test_has_transparency_handles_none():
    assert processor.has_transparency(None) is False


# ---------------------------------------------------------------------------
# 3. 中文错误信息（红线 5）
# ---------------------------------------------------------------------------
def test_error_message_is_chinese_and_single_line():
    message = processor.format_error_message("bad.jpg", OSError("cannot identify\nimage file"))
    assert message.startswith("处理失败：bad.jpg")
    assert "cannot identify" in message
    assert "\n" not in message
    # 不允许出现英文堆栈关键字
    for forbidden in ("Traceback", "File \"", "Error:"):
        assert forbidden not in message


def test_describe_summary_is_chinese():
    text = processor.describe_summary({"total": 3, "success": 2, "failed": 1})
    assert "共 3 张" in text
    assert "成功 2 张" in text
    assert "失败 1 张" in text
    assert "Traceback" not in text


def test_describe_summary_handles_empty_result():
    assert processor.describe_summary({}) == "没有需要处理的图片。"


def test_processor_module_does_not_require_pillow_to_import():
    """processor 使用延迟导入，缺少 Pillow 时模块本身仍然可以被导入和测试。"""
    assert "PIL" not in processor.__dict__
    assert processor.build_save_options(utils.FORMAT_JPEG, 80)["quality"] == 80


def test_handle_one_image_returns_dict_with_chinese_error(tmp_path):
    """handle_one_image 返回结构化字典：失败时 result 是中文提示，绝不抛异常。"""
    try:
        import PIL  # noqa: F401
    except Exception:
        pytest.skip("环境缺少 Pillow，跳过依赖图片库的用例")

    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"definitely-not-an-image")

    outcome = processor.handle_one_image(
        str(broken), str(tmp_path), utils.FORMAT_JPEG, 80
    )
    assert set(outcome.keys()) == {"success", "result", "note", "notes", "format"}
    assert outcome["success"] is False
    assert "处理失败" in outcome["result"]
    assert outcome["note"] == ""
    assert "Traceback" not in outcome["result"]


def test_process_one_image_still_returns_three_tuple(tmp_path):
    """兼容入口必须保持三元组，避免破坏界面与既有调用方。"""
    try:
        import PIL  # noqa: F401
    except Exception:
        pytest.skip("环境缺少 Pillow，跳过依赖图片库的用例")

    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"definitely-not-an-image")

    success, result, note = processor.process_one_image(
        str(broken), str(tmp_path), utils.FORMAT_JPEG, 80
    )
    assert success is False
    assert "处理失败" in result
    assert note == ""


# ---------------------------------------------------------------------------
# 4. 批量处理：遍历、跳过、容错、进度
# ---------------------------------------------------------------------------
def make_files(tmp_path, names):
    for name in names:
        (tmp_path / name).write_bytes(b"not-a-real-image")
    return tmp_path


def test_processor_skips_unsupported_files(tmp_path):
    """扩展名不在支持列表里的文件（.txt / .psd）直接跳过；.gif 现在属于支持格式。"""
    make_files(tmp_path, ["a.jpg", "notes.txt", "c.psd", "d.png", "e.gif"])
    files = utils.list_image_files(str(tmp_path))

    handled = []
    processor_obj = processor.BatchProcessor(
        files=files,
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        on_file_done=lambda payload: handled.append(payload["source_name"]),
    )
    result = processor_obj.run()

    assert handled == ["a.jpg", "d.png", "e.gif"]  # 非图片文件被跳过，gif 受支持
    assert result["total"] == 3


def test_processor_continues_after_single_failure(tmp_path, monkeypatch):
    """红线 4：单张图片失败只记录日志，绝不中断整个队列。"""
    make_files(tmp_path, ["good1.jpg", "broken.jpg", "good2.png", "good3.jpg"])

    def fake_process(source_path, output_dir, selected_format, quality):
        if os.path.basename(source_path) == "broken.jpg":
            return False, "处理失败：broken.jpg（原因：图片已损坏或格式不正确）", ""
        return True, os.path.join(output_dir, os.path.basename(source_path)), ""

    monkeypatch.setattr(processor, "process_one_image", fake_process)

    logs = []
    processor_obj = processor.BatchProcessor(
        files=utils.list_image_files(str(tmp_path)),
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        on_log=logs.append,
    )
    result = processor_obj.run()

    assert result["total"] == 4
    assert result["success"] == 3
    assert result["failed"] == 1
    assert "broken.jpg" in result["failures"][0]
    # 失败图片之后的图片依然被处理
    assert any("good3.jpg" in line for line in logs)


def test_processor_reports_every_file_once(tmp_path, monkeypatch):
    """进度必须是「每处理完一张就报一次」，供进度条使用。"""
    make_files(tmp_path, ["1.jpg", "2.jpg", "3.png"])

    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda source_path, output_dir, selected_format, quality: (True, source_path, ""),
    )

    progress = []
    processor_obj = processor.BatchProcessor(
        files=utils.list_image_files(str(tmp_path)),
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        on_file_done=lambda payload: progress.append((payload["processed"], payload["total"])),
    )
    processor_obj.run()

    assert progress == [(1, 3), (2, 3), (3, 3)]


# ---------------------------------------------------------------------------
# 5. 取消粒度（红线 3）
# ---------------------------------------------------------------------------
class CancelCounter:
    """模拟取消标志：记录被检查了多少次，第 N 次检查后返回 True。"""

    def __init__(self, cancel_after_checks=None):
        self.calls = 0
        self.cancel_after_checks = cancel_after_checks

    def __call__(self):
        self.calls += 1
        if self.cancel_after_checks is None:
            return False
        return self.calls > self.cancel_after_checks


def test_cancel_is_checked_once_per_image(tmp_path, monkeypatch):
    """取消只在每张图片处理完成后检查一次，共 N 张图就检查 N 次。"""
    make_files(tmp_path, ["1.jpg", "2.jpg", "3.jpg"])
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda source_path, output_dir, selected_format, quality: (True, source_path, ""),
    )

    counter = CancelCounter()
    processor_obj = processor.BatchProcessor(
        files=utils.list_image_files(str(tmp_path)),
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        cancel_check=counter,
    )
    processor_obj.run()

    assert counter.calls == 3


def test_cancel_stops_before_next_image(tmp_path, monkeypatch):
    """取消请求最多让当前图片处理完，不会继续处理下一张。"""
    make_files(tmp_path, ["1.jpg", "2.jpg", "3.jpg"])
    handled = []

    def fake_process(source_path, output_dir, selected_format, quality):
        handled.append(os.path.basename(source_path))
        return True, source_path, ""

    monkeypatch.setattr(processor, "process_one_image", fake_process)

    processor_obj = processor.BatchProcessor(
        files=utils.list_image_files(str(tmp_path)),
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        cancel_check=CancelCounter(cancel_after_checks=1),
    )

    with pytest.raises(processor.BatchCancelledError):
        processor_obj.run()

    assert handled == ["1.jpg"]  # 当前图片处理完就停，第 2、3 张不再处理


def test_cancel_before_start_handles_nothing(tmp_path, monkeypatch):
    make_files(tmp_path, ["1.jpg"])
    handled = []
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda *args, **kwargs: handled.append(args[0]) or (True, args[0], ""),
    )

    processor_obj = processor.BatchProcessor(
        files=["1.jpg"],
        input_path=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        cancel_check=lambda: True,
    )

    with pytest.raises(processor.BatchCancelledError):
        processor_obj.run()
    assert handled == []


# ---------------------------------------------------------------------------
# 6. 后台线程事件流（替代真实的 queue.Queue -> 主线程 after 链路）
# ---------------------------------------------------------------------------
def make_worker(tmp_path, **kwargs):
    events = []
    worker = processor.BatchWorker(
        input_path=str(tmp_path),
        selected_format=kwargs.pop("selected_format", utils.FORMAT_KEEP),
        quality=kwargs.pop("quality", 80),
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )
    for key, value in kwargs.items():
        setattr(worker, key, value)
    return worker, events


def test_worker_emits_progress_and_finished(tmp_path, monkeypatch):
    # 用 .txt 作为「不受支持的文件」；.gif 现在已是受支持格式
    make_files(tmp_path, ["a.jpg", "b.jpg", "c.png", "skip.txt"])
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda source_path, output_dir, selected_format, quality: (True, source_path, ""),
    )

    worker, events = make_worker(tmp_path)
    thread = worker.start()
    assert thread.name == "imagetool-worker"
    assert thread.daemon is True  # 主窗口关闭时线程不会阻塞退出
    thread.join(timeout=10)

    event_types = [event_type for event_type, _payload in events]
    assert processor.EVENT_PROGRESS in event_types
    assert processor.EVENT_FINISHED in event_types
    assert processor.EVENT_FILE_DONE in event_types
    assert event_types.count(processor.EVENT_FILE_DONE) == 3

    finished = [payload for event_type, payload in events
                if event_type == processor.EVENT_FINISHED][0]
    assert (finished["total"], finished["success"], finished["failed"]) == (3, 3, 0)


def test_worker_creates_compressed_subfolder(tmp_path, monkeypatch):
    make_files(tmp_path, ["a.jpg"])
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda source_path, output_dir, selected_format, quality: (True, source_path, ""),
    )

    worker, events = make_worker(tmp_path)
    worker.start().join(timeout=10)

    assert os.path.isdir(os.path.join(str(tmp_path), utils.OUTPUT_SUBDIR_NAME))
    logs = [payload for event_type, payload in events if event_type == processor.EVENT_LOG]
    assert any("共 1 张图片" in line for line in logs)


def test_worker_with_no_images_finishes_cleanly(tmp_path):
    (tmp_path / "readme.txt").write_text("hello", encoding="utf-8")

    worker, events = make_worker(tmp_path)
    worker.start().join(timeout=10)

    event_types = [event_type for event_type, _payload in events]
    # 空输入时 worker 先报总数 0 与日志，再报结束；顺序不作强约束，只校验内容
    assert processor.EVENT_PROGRESS in event_types
    assert processor.EVENT_FINISHED in event_types
    assert processor.EVENT_FILE_DONE not in event_types
    log_messages = [payload for event_type, payload in events
                    if event_type == processor.EVENT_LOG]
    assert any("没有找到需要处理的 JPG/PNG" in text for text in log_messages)


def test_worker_emits_folder_error_event_on_permission_denied(tmp_path, monkeypatch):
    """红线 8：无法创建子文件夹时发出 folder_error 事件，由主线程弹中文提示。"""
    make_files(tmp_path, ["a.jpg"])

    def fake_make_output_directory(_parent_dir):
        raise utils.SubfolderCreationError("无法在所选文件夹创建子文件夹，请换一个文件夹")

    monkeypatch.setattr(processor, "make_output_directory", fake_make_output_directory)
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda *args, **kwargs: pytest.fail("创建子文件夹失败时不应继续处理图片"),
    )

    worker, events = make_worker(tmp_path)
    worker.start().join(timeout=10)

    event_types = [event_type for event_type, _payload in events]
    # 先上报日志与统一进度基准，再报无法创建子文件夹（红线 8）
    assert event_types == [
        processor.EVENT_PROGRESS,
        processor.EVENT_LOG,
        processor.EVENT_FOLDER_ERROR,
    ]
    assert "无法在所选文件夹创建子文件夹" in events[-1][1]["message"]


def test_worker_survives_emit_errors(tmp_path, monkeypatch):
    """界面已销毁导致回调抛异常时，工作线程不能崩溃。"""
    make_files(tmp_path, ["a.jpg"])
    monkeypatch.setattr(
        processor,
        "process_one_image",
        lambda source_path, output_dir, selected_format, quality: (True, source_path, ""),
    )

    def broken_emit(_event_type, _payload):
        raise RuntimeError("窗口已关闭")

    worker = processor.BatchWorker(
        input_path=str(tmp_path),
        selected_format=utils.FORMAT_KEEP,
        quality=80,
        emit=broken_emit,
    )
    thread = worker.start()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_worker_cancel_event_is_thread_safe(tmp_path, monkeypatch):
    """request_cancel 只置标志位，处理循环据此在间隙停止。"""
    make_files(tmp_path, ["1.jpg", "2.jpg", "3.jpg"])

    handled = []

    def fake_process(source_path, output_dir, selected_format, quality):
        handled.append(os.path.basename(source_path))
        worker.request_cancel()  # 模拟用户在处理过程中点击取消
        return True, source_path, ""

    worker, events = make_worker(tmp_path)
    monkeypatch.setattr(processor, "process_one_image", fake_process)

    worker.start().join(timeout=10)

    event_types = [event_type for event_type, _payload in events]
    assert processor.EVENT_CANCELLED in event_types
    assert processor.EVENT_FINISHED not in event_types
    assert handled == ["1.jpg"]
