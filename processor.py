# -*- coding: utf-8 -*-
"""核心业务逻辑模块。

只负责「怎么处理图片」，完全不依赖 Tkinter：
- processor.has_transparency / normalize_image_mode：透明图转 JPEG 时铺白底
- processor.correct_exif_orientation：EXIF 旋转校正
- processor.build_save_options：不同格式的保存参数映射
- processor.process_one_image：单张图片的处理（失败只记录，不抛出）
- processor.resolve_input_files / get_input_image_total：统一「文件夹 / 文件列表」两种输入
- processor.process_images：单一入口，内部用 isinstance(input_path, list) 分流
- processor.BatchProcessor：批量遍历 + 取消粒度控制（每张之间检查）
- processor.BatchWorker：后台线程 + 事件回调（由界面转成 queue.Queue）

所有对外错误信息均为中文，不把英文堆栈抛给用户。
"""

import inspect
import os
import threading

from utils import (
    BMP_EXTENSIONS,
    FORMAT_BMP,
    FORMAT_GIF,
    FORMAT_JPEG,
    FORMAT_PNG,
    FORMAT_TIFF,
    FORMAT_WEBP,
    GIF_EXTENSIONS,
    JPEG_EXTENSIONS,
    MULTI_FRAME_FORMATS,
    OUTPUT_SUBDIR_NAME,
    PNG_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TIFF_EXTENSIONS,
    WEBP_EXTENSIONS,
    SubfolderCreationError,
    build_output_path,
    clamp_quality,
    filter_existing_images,
    format_error_message,
    get_file_extension,
    get_format_extension,
    get_output_directory_for_input,
    list_image_files,
    make_output_directory,
    quality_to_compress_level,
    resolve_target_format,
    shorten_message,
)

# TIFF 走有损 JPEG 压缩时的质量上限（避免用户把质量拉到 100 导致体积暴涨）
TIFF_JPEG_QUALITY_CAP = 95

# 「检测到多帧图片」提示的去重记录：每个文件路径只提示一次，绝不重复刷屏
_MULTI_FRAME_NOTICE_LOGGED = set()

# ---------------------------------------------------------------------------
# 事件类型常量（界面与处理器之间的通信协议）
# ---------------------------------------------------------------------------
EVENT_LOG = "log"            # 普通日志文本
EVENT_PROGRESS = "progress"  # 进度更新
EVENT_FILE_DONE = "file_done"  # 单张图片处理结束
EVENT_FOLDER_ERROR = "folder_error"  # 无法创建输出子文件夹（需要弹窗并终止）
EVENT_FINISHED = "finished"  # 任务整体结束
EVENT_CANCELLED = "cancelled"  # 用户取消，任务提前结束

__all__ = [
    "BatchCancelledError",
    "BatchProcessor",
    "BatchWorker",
    "EVENT_CANCELLED",
    "EVENT_FILE_DONE",
    "EVENT_FINISHED",
    "EVENT_FOLDER_ERROR",
    "EVENT_LOG",
    "EVENT_PROGRESS",
    "build_save_options",
    "correct_exif_orientation",
    "describe_summary",
    "get_input_image_total",
    "handle_one_image",
    "has_transparency",
    "load_image_for_processing",
    "normalize_image_mode",
    "process_images",
    "process_one_image",
    "resolve_input_files",
    "resolve_output_base_directory",
    "resolve_output_format",
    "save_image_with_fallback",
    "seek_first_frame",
    "TIFF_JPEG_QUALITY_CAP",
]


def build_save_options(target_format, quality):
    """根据**最终输出格式**生成 Pillow 的保存参数。

    关键点：这里的 target_format 一定是「用户选择或降级之后」的目标格式，
    绝不使用原图格式——否则会出现「原图是 PNG、但实际要存 JPEG」时参数错配。

    - JPEG / WebP：传递 quality（1-100），WebP 用有损模式
    - PNG：把 1-100 映射成 compress_level（0-9），绝不传 quality
    - GIF：不支持 quality，只做 optimize（传 quality 会被静默忽略或报错）
    - TIFF：用无损 LZW 压缩；给 quality 时走 JPEG 压缩并限制质量上限
    """
    quality = clamp_quality(quality)
    if target_format in (FORMAT_JPEG, FORMAT_WEBP):
        return {"quality": quality, "optimize": quality >= 60}
    if target_format == FORMAT_PNG:
        return {"compress_level": quality_to_compress_level(quality)}
    if target_format == FORMAT_GIF:
        # GIF 的调色板格式不支持 quality，直接忽略质量值
        return {"optimize": quality >= 60}
    if target_format == FORMAT_TIFF:
        # 质量很低时才启用有损 JPEG 压缩，并限制在上限内，避免出现块状噪点
        if quality < 90:
            return {"compression": "tiff_lzw"}
        return {"compression": "jpeg", "quality": min(quality, TIFF_JPEG_QUALITY_CAP)}
    return {}


def has_transparency(image):
    """判断图片是否带透明通道。

    覆盖 RGBA / LA / PA / P 调色板带透明色 / RGBa 预乘透明等常见情况。
    注意：alpha 通道可能出现在任意位置（RGBA / LA / PA / CMYKA），
    不能写死成 mode[3] == 'A'，否则 "LA" 这类两字符模式会被漏判。
    """
    if image is None:
        return False
    mode = (getattr(image, "mode", "") or "").upper()
    if "A" in mode:  # RGBA / LA / PA / CMYKA / RGBa
        return True
    if mode == "P":
        info = getattr(image, "info", None) or {}
        if "transparency" in info:
            return True
    return False


def normalize_image_mode(image, target_format):
    """把图片转换成目标格式能接受的色彩模式。

    红线 1：透明图转 JPEG 时不能直接 convert('RGB')，
    必须先建一张白色背景的 RGB 新图，再把原图作为遮罩粘贴上去，
    否则透明区域会变成黑色。
    """
    if image is None:
        return image
    mode = (getattr(image, "mode", "") or "").upper()

    if target_format == FORMAT_JPEG:
        if mode in ("RGB", "L"):
            # 灰度图和 RGB 图可以直接保存，无需拷贝
            return image
        if has_transparency(image):
            rgba_image = image if mode in ("RGBA", "LA", "PA") else image.convert("RGBA")
            white_background = _new_white_rgb(rgba_image.size)
            if rgba_image.mode == "LA":
                rgba_image = rgba_image.convert("RGBA")
            white_background.paste(rgba_image, mask=rgba_image.split()[-1])
            return white_background
        # CMYK / P / I;16 等无透明通道的情况，直接转 RGB 即可
        return image.convert("RGB")

    if target_format == FORMAT_PNG:
        # PNG 支持几乎所有模式，保持原样输出，避免无意义的重采样
        return image
    return image


def _new_white_rgb(size):
    """创建一张纯白色背景的 RGB 新图（独立函数，方便单元测试）。"""
    from PIL import Image  # 延迟导入，保证缺少 Pillow 时模块本身仍可被导入

    return Image.new("RGB", size, (255, 255, 255))


def correct_exif_orientation(image):
    """使用 ImageOps.exif_transpose() 校正手机照片的 EXIF 旋转方向。

    兼容性处理：
    - 老版本 Pillow 没有该方法时，原样返回
    - 有的版本在没有 EXIF 信息时会返回 None，此时回退到原图
    """
    if image is None:
        return image
    try:
        from PIL import ImageOps
    except ImportError:
        return image
    transpose = getattr(ImageOps, "exif_transpose", None)
    if transpose is None:
        return image
    try:
        corrected = transpose(image)
    except Exception:
        # EXIF 损坏时不值得让整张图失败，保持原方向继续处理
        return image
    if corrected is None:
        return image
    return corrected


def _count_frames(image):
    """读取图片总帧数；取不到或出错时按 1 帧处理。"""
    try:
        return int(getattr(image, "n_frames", 1))
    except Exception:
        return 1


def load_image_for_processing(source_path):
    """读取图片并完成 EXIF 旋转校正。

    返回 (image, detected_format, multi_frame)：
    - detected_format 是文件**真实的编码格式**（如 "PNG" / "JPEG"），
      用于处理「扩展名与实际内容不一致」的情况（例如 .jpg 实际是 PNG）；
    - multi_frame 表示原文件是否包含多帧。

    注意：ImageOps.exif_transpose() 会返回一个**新对象**，新对象上不再有
    n_frames 属性，所以帧数必须在转置之前读出来。
    """
    from PIL import Image  # 延迟导入

    image = Image.open(source_path)
    frame_count = _count_frames(image)   # 必须在 exif_transpose 之前读取
    image.load()
    detected_format = (image.format or "").upper()
    if detected_format == "MPO":  # 部分手机连拍文件以 MPO 容器存储，本质仍是 JPEG
        detected_format = "JPEG"
    image = correct_exif_orientation(image)
    return image, detected_format, frame_count > 1


def seek_first_frame(image, source_path=None, notice_logged=None, multi_frame=None):
    """把多帧图片定位到第一帧。

    Pillow 打开 GIF / TIFF 后默认停在第一帧，但为了语义明确（以及避免后续
    操作把当前帧当作其它帧），这里显式执行 seek(0)。

    :param multi_frame: 是否为多帧图片。由于 exif_transpose 会丢掉 n_frames，
        这个标志通常由 load_image_for_processing 在转置前算好传进来；
        不传时回退到读取 image.n_frames。

    返回提示文本（单帧图片返回空串）：
    - 每个文件只在**首次**处理时给出一条提示，重复处理同一文件不再提示，
      避免批量循环里刷屏。
    - notice_logged 可传入外部集合用于记录已提示过的文件，默认用模块级集合。
    """
    if image is None:
        return ""
    seek = getattr(image, "seek", None)
    if seek is not None:
        try:
            seek(0)
        except Exception:
            # 损坏的多帧文件：定位失败不影响后面的处理，交给保存阶段报错
            return ""
    if multi_frame is None:
        multi_frame = _info_has_multiple_frames(image)
    if not multi_frame:
        return ""

    key = str(source_path) if source_path is not None else ""
    logged = _MULTI_FRAME_NOTICE_LOGGED if notice_logged is None else notice_logged
    if key and key in logged:
        return ""  # 同一个文件已经提示过，绝不重复输出
    if key:
        logged.add(key)
    return "检测到多帧图片，仅处理第一帧"


def resolve_output_format(image, source_path, selected_format, detected_format):
    """决定最终输出格式。

    规则：
    - 用户选择「原格式」：以文件**真实编码格式**为准，这样扩展名骗人
      （例如 .jpg 实际是 PNG）时也不会存错格式；真实格式未知时退回扩展名判断。
    - 用户明确选择 JPEG / PNG：尊重用户选择，只有一种例外——
      图像真的带透明通道（RGBA / LA / 带透明色的调色板图）时不允许转 JPEG，
      否则透明信息会永久丢失、或者被填成白底，两种结果都不是用户想要的。
    返回 FORMAT_JPEG / FORMAT_PNG，无法判断时返回 None。
    """
    if selected_format in (FORMAT_JPEG, FORMAT_PNG):
        if selected_format == FORMAT_JPEG and has_transparency(image):
            return FORMAT_PNG
        return selected_format
    if detected_format == "PNG":
        return FORMAT_PNG
    if detected_format in ("JPEG", "JPG"):
        return FORMAT_JPEG
    return resolve_target_format(source_path, selected_format)


# ---------------------------------------------------------------------------
# 多帧图片处理（GIF / TIFF 只取第一帧）
# ---------------------------------------------------------------------------
def _info_has_multiple_frames(image):
    """判断图片是否是多帧（GIF / TIFF 等）。取不到 n_frames 时视为单帧。

    注意：EXIF 转置之后的图片对象没有 n_frames 属性，
    这种场景请改用 _count_frames() 在转置**之前**取得的帧数。
    """
    return _count_frames(image) > 1


# ---------------------------------------------------------------------------
# 保存（含特殊格式降级为 JPEG）
# ---------------------------------------------------------------------------
def _emit_note(emit, text):
    """安全地发出日志/提示：emit 为空或已失效时静默忽略。"""
    if emit is None:
        return
    try:
        emit(text)
    except Exception:
        pass


def _supports_emit(func):
    """判断函数是否接受 emit 关键字参数。

    这样既能让真实实现收到「保存失败已降级」的实时日志，
    也不会打断只接受 4 个位置参数的测试替身。
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    if "emit" in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _call_process_one_image(source_path, output_dir, selected_format, quality, emit):
    """调用 process_one_image，并在其支持时传入 emit 回调。"""
    if _supports_emit(process_one_image):
        return process_one_image(source_path, output_dir, selected_format, quality,
                                 emit=emit)
    return process_one_image(source_path, output_dir, selected_format, quality)


def save_image_with_fallback(image, output_dir, source_path, target_format, quality,
                             emit=None):
    """按目标格式保存；失败时降级为 JPEG。

    某些环境缺少 WebP / TIFF / GIF 的编解码器，保存会抛异常。此时：
    1. 用中文提示告知用户「已自动转为 JPEG 保存」（并保留原始报错便于排查）；
    2. 强制转存 JPEG，输出后缀同步改成 .jpg，并复用 (1)、(2) 重名逻辑；
    3. 带透明通道的图复用「白色背景 + 遮罩粘贴」逻辑，避免透明区域变黑。

    返回 (输出路径, 实际使用的格式, 提示文本)。
    """
    # 关键：始终先按「最终输出格式」归一化色彩模式。
    # 漏掉这一步的话，GIF（P 调色板模式）存 JPEG 会直接报
    # "cannot write mode P as JPEG"；RGBA 存 JPEG 也会失败。
    normalized_image = normalize_image_mode(image, target_format)
    try:
        try:
            save_kwargs = dict(build_save_options(target_format, quality))
            save_kwargs["format"] = target_format
            path = build_output_path(output_dir, source_path, target_format)
            normalized_image.save(path, **save_kwargs)
            return path, target_format, ""
        except Exception as exc:  # noqa: BLE001 - 缺少编解码器 / 参数不支持 / 磁盘错误
            detail = shorten_message(exc)

        original_format = target_format
        if original_format in (FORMAT_JPEG, FORMAT_PNG):
            # 这两种格式的编解码器一定存在，失败说明是磁盘/权限等真实问题；
            # 再转 JPEG 只会重复失败，如实报错让上层记录，避免无效重试。
            raise ValueError("保存 {0} 失败：{1}".format(original_format, detail))

        extra_note = ""
        if has_transparency(image):
            # 走正常流程时不会在这里铺白底（WebP/GIF/TIFF 都支持透明），
            # 但降级成 JPEG 就必须先铺白底，否则透明区域会变黑
            extra_note = "（原图带透明通道，已自动铺白底）"

        fallback_image = normalize_image_mode(normalized_image, FORMAT_JPEG)
        try:
            fallback_kwargs = dict(build_save_options(FORMAT_JPEG, quality))
            fallback_kwargs["format"] = FORMAT_JPEG
            # 关键：用 JPEG 扩展名生成路径，保证 photo.webp -> photo.jpg，
            # 同时复用既有的 (1)、(2) 重名逻辑
            output_path = build_output_path(output_dir, source_path, FORMAT_JPEG)
            fallback_image.save(output_path, **fallback_kwargs)
        finally:
            if fallback_image is not normalized_image:
                try:
                    fallback_image.close()
                except Exception:
                    pass

        source_ext = get_file_extension(source_path) or get_format_extension(original_format)
        _emit_note(emit, "保存为 {0} 失败（原因：{1}），已自动转为 JPEG。".format(
            source_ext, detail
        ))
        return output_path, FORMAT_JPEG, (
            "当前环境不支持保存为 {0} 格式，已自动转为 JPEG 保存{1}".format(
                source_ext, extra_note
            )
        )
    finally:
        if normalized_image is not image:
            try:
                normalized_image.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 统一输入解析：input_path 既可能是文件夹路径，也可能是文件列表
# ---------------------------------------------------------------------------
def resolve_input_files(input_path):
    """统一解析输入：凡是列表就当作文件列表，其它情况当作文件夹路径。

    使用 isinstance(input_path, list) 分流（子类如 list 的子类同样命中）。
    返回**文件名或文件路径**的列表；文件夹模式下返回的只是文件名。
    """
    if isinstance(input_path, list):
        return list(input_path)
    return list_image_files(input_path)


def get_input_image_total(input_path):
    """返回该输入的图片总数，供界面设置进度条最大值（max）。"""
    return len(resolve_input_files(input_path))


# ---------------------------------------------------------------------------
# 单张图片处理
# ---------------------------------------------------------------------------
def handle_one_image(source_path, output_dir, selected_format, quality, emit=None):
    """处理单张图片，返回结构化结果字典。

    返回 {"success": bool, "result": 输出路径或中文错误信息,
          "note": 提示文本（多条用换行拼接，供日志显示）,
          "notes": 提示文本列表, "format": 实际使用的输出格式}。
    单张失败只返回错误文本，绝不抛出，保证批量队列不会中断。

    :param emit: 可选日志回调（用于「保存失败已降级」这类即时提示）
    """
    image = None
    try:
        image, detected_format, multi_frame = load_image_for_processing(source_path)
        target_format = resolve_output_format(image, source_path, selected_format, detected_format)
        if target_format is None:
            return {
                "success": False,
                "result": "处理失败：{0}（原因：无法识别的图片格式）".format(
                    os.path.basename(str(source_path))
                ),
                "note": "",
                "notes": [],
                "format": None,
            }

        notes = []
        # 多帧图片（GIF / TIFF）只处理第一帧，每个文件只提示一次
        frame_note = seek_first_frame(image, source_path, multi_frame=multi_frame)
        if frame_note:
            notes.append(frame_note)

        final_format = target_format
        if selected_format == FORMAT_JPEG and target_format == FORMAT_PNG:
            notes.append("该图片带透明通道，无法保存为 JPEG，已按 PNG 输出以保留透明区域。")

        # 按「最终输出格式」决定保存参数，并在失败时降级为 JPEG
        output_path, final_format, degrade_note = save_image_with_fallback(
            image, output_dir, source_path, target_format, quality, emit=emit
        )
        if degrade_note:
            notes.append(degrade_note)

        return {
            "success": True,
            "result": output_path,
            "note": "\n".join(notes),
            "notes": notes,
            "format": final_format,
        }
    except Exception as exc:  # noqa: BLE001 - 单张图片任何异常都不能中断批量任务
        return {
            "success": False,
            "result": format_error_message(os.path.basename(str(source_path)), exc),
            "note": "",
            "notes": [],
            "format": None,
        }
    finally:
        if image is not None:
            try:
                image.close()
            except Exception:
                pass


def process_one_image(source_path, output_dir, selected_format, quality, emit=None):
    """兼容入口：返回 (是否成功, 输出路径或错误信息, 提示信息) 三元组。

    界面与既有测试都使用这个形状，内部复用 handle_one_image。
    """
    outcome = handle_one_image(source_path, output_dir, selected_format, quality, emit=emit)
    return outcome["success"], outcome["result"], outcome["note"]


def resolve_output_base_directory(input_path, files=None):
    """计算输出用的**基准目录**（注意：不包含 Compressed 子目录名）。

    - 文件列表模式：以「列表中第一张图片所在文件夹」为基准，方便用户找到结果；
    - 文件夹模式：以该文件夹为基准。
    子目录名统一由 utils.make_output_directory() 添加，避免重复拼成 Compressed\\Compressed。
    输入为空时返回 None。
    """
    base_dir = get_output_directory_for_input(input_path)
    if base_dir is not None:
        return base_dir
    file_list = list(files) if files is not None else resolve_input_files(input_path)
    if not file_list:
        return None
    first_file = str(file_list[0])
    if not os.path.isabs(first_file) and not isinstance(input_path, list):
        first_file = os.path.join(str(input_path), first_file)
    return os.path.dirname(os.path.abspath(first_file))


# ---------------------------------------------------------------------------
# 单一入口：process_images
# ---------------------------------------------------------------------------
def process_images(input_path, quality=80, format=FORMAT_JPEG, callback=None, cancel_event=None):
    """统一处理入口。

    :param input_path: 文件夹路径字符串，或图片文件路径列表（内部用 isinstance 分流）
    :param quality: 1-100 压缩质量
    :param format: "JPEG" / "PNG" / "keep"
    :param callback: 进度回调，每处理完一张调用一次，参数是字典：
                     {"index", "total", "source", "output", "success", "message"}
    :param cancel_event: threading.Event，用于请求取消；
                     只在每张图片之间的间隙检查，不强行中断 Pillow。
    :return: 统计字典 {"total", "processed", "success", "failed",
                      "failures", "cancelled"}

    说明：
    - 进度基准统一取「待处理图片数量」，文件列表模式的最大值就是列表长度；
    - 单张失败只记录不中断；
    - 无法创建输出子文件夹时抛出 SubfolderCreationError，由调用方弹中文提示。
    """
    files = resolve_input_files(input_path)
    summary = {
        "total": len(files),
        "processed": 0,
        "success": 0,
        "failed": 0,
        "failures": [],
        "cancelled": False,
    }
    if not files:
        return summary

    _emit_log(callback, "共 {0} 张图片，开始处理。".format(len(files)))
    # make_output_directory 会在基准目录下创建 Compressed 子文件夹
    output_dir = make_output_directory(resolve_output_base_directory(input_path, files))

    for index, source in enumerate(files, start=1):
        # 取消粒度：只在每张图片开始前的间隙检查
        if cancel_event is not None and cancel_event.is_set():
            summary["cancelled"] = True
            break

        if isinstance(input_path, list):
            source_path = str(source)  # 文件列表里已经是完整路径
        else:
            source_path = os.path.join(str(input_path), str(source))
        source_name = os.path.basename(source_path)

        _emit_log(callback, "正在处理：{0}".format(source_name))
        # 统一走 process_one_image（内部委托 handle_one_image），保证只有一条处理路径。
        # 返回 4 元组（含提示列表）；为兼容只返回 3 元组的测试替身，这里做长度兼容。
        outcome = _call_process_one_image(
            source_path, output_dir, format, quality,
            emit=lambda text: _emit_log(callback, text),
        )
        if len(outcome) == 4:
            success, result, note, notes = outcome
        else:
            success, result, note = outcome
            notes = [note] if note else []

        if success:
            summary["success"] += 1
            _emit_log(callback, "成功：{0} -> {1}".format(
                source_name, os.path.basename(str(result))
            ))
            # 逐条提示（多帧、降级等）；降级信息在保存阶段已经实时打印过，这里跳过避免重复
            for item in notes:
                if item and not item.startswith("当前环境不支持保存为"):
                    _emit_log(callback, "说明：{0}（{1}）".format(source_name, item))
        else:
            summary["failed"] += 1
            summary["failures"].append(result)
            _emit_log(callback, result)

        summary["processed"] = index
        if callback is not None:
            callback({
                "index": index,
                "total": len(files),
                "source": source_name,
                "output": result if success else "",
                "success": success,
                "message": note if success else result,
            })

    return summary


def _emit_log(callback, text):
    """通过 callback 上报日志；界面只关心带 "log" 键的字典，其它回调自动忽略。"""
    if callback is not None:
        callback({"log": text})


# ---------------------------------------------------------------------------
# 批量处理器 / 后台线程（沿用既有事件协议，内部改为调用 process_images）
# ---------------------------------------------------------------------------
class BatchCancelledError(Exception):
    """内部使用：表示用户取消了任务。"""


class BatchProcessor:
    """批量处理器：负责遍历、进度统计与取消粒度控制。

    取消粒度（红线 3）：只在每张图片处理完成后的循环间隙检查取消标志，
    当前正在处理的大图会先处理完，不强行中断 Pillow。

    进度基准（统一）：total 恒等于「待处理图片数量」，
    文件列表模式下就是列表长度，保证界面进度条平滑推进。
    """

    def __init__(self, files, input_dir=None, output_dir=None, selected_format=FORMAT_JPEG,
                 quality=80, emit=None, on_file_done=None, on_log=None, cancel_check=None,
                 input_path=None):
        self.files = list(files)
        # 优先使用统一的 input_path（可能是文件夹路径或文件列表），
        # 保留 input_dir 作为兼容写法；两者都不给则按「文件列表模式」处理。
        if input_path is not None:
            self.input_path = input_path
        else:
            self.input_path = input_dir
        self.input_dir = self.input_path
        self.output_dir = output_dir
        self.selected_format = selected_format
        self.quality = quality
        self.emit = emit
        self.on_file_done = on_file_done
        self.on_log = on_log
        self.cancel_check = cancel_check or (lambda: False)
        self.total = len(self.files)
        self.success_count = 0
        self.failed_count = 0
        self.failure_messages = []
        self.processed = 0
        self.cancelled = False

    def get_total(self):
        """进度基准：待处理图片数量（供界面设置进度条 max）。"""
        return self.total

    def run(self):
        """执行批量处理，返回统计结果字典。"""
        if self.input_path is None:
            return self._run_file_list()
        return self._run_folder()

    # -- 文件夹模式 -------------------------------------------------------
    def _run_folder(self):
        summary = process_images(
            self.input_path,
            quality=self.quality,
            format=self.selected_format,
            callback=self._on_progress_payload,
            cancel_event=_CancelFlag(self.cancel_check),
        )
        self._apply(summary)
        if summary["cancelled"]:
            raise BatchCancelledError()
        return self._summary()

    # -- 文件列表模式 -----------------------------------------------------
    def _run_file_list(self):
        output_dir = self.output_dir or make_output_directory(
            resolve_output_base_directory(self.files, self.files)
        )
        for index, source_path in enumerate(self.files, start=1):
            if self.cancel_check():
                self.cancelled = True
                raise BatchCancelledError()
            source_path = str(source_path)
            source_name = os.path.basename(source_path)
            self._emit_log("正在处理：{0}".format(source_name))
            try:
                # 传 emit 是为了让「保存失败已降级」这类信息能实时打印
                success, result, note = _call_process_one_image(
                    source_path, output_dir, self.selected_format, self.quality,
                    emit=self._emit_log,
                )
            except Exception as exc:  # 双保险：process_one_image 理论上不抛异常
                success, result, note = False, format_error_message(source_name, exc), ""

            if success:
                self.success_count += 1
                self._emit_log("成功：{0} -> {1}".format(
                    source_name, os.path.basename(str(result))
                ))
                if note:
                    self._emit_log("说明：{0}（{1}）".format(source_name, note))
            else:
                self.failed_count += 1
                self.failure_messages.append(result)
                self._emit_log(result)

            self.processed = index
            if self.on_file_done is not None:
                self.on_file_done({
                    "source_name": source_name,
                    "success": success,
                    "result": result,
                    "processed": index,
                    "total": self.total,
                })
            if self.emit is not None:
                self.emit(EVENT_PROGRESS, {"index": index, "total": self.total})
        return self._summary()

    # -- 进度回调 ---------------------------------------------------------
    def _on_progress_payload(self, payload):
        if "log" in payload:
            self._emit_log(payload["log"])
            return
        self.processed = int(payload.get("index", 0) or 0)
        if payload.get("success"):
            self.success_count += 1
        else:
            self.failed_count += 1
            if payload.get("message") and payload["message"] not in self.failure_messages:
                self.failure_messages.append(payload["message"])
        if self.on_file_done is not None:
            self.on_file_done({
                "source_name": payload.get("source", ""),
                "success": bool(payload.get("success")),
                "result": payload.get("output") or payload.get("message") or "",
                "processed": self.processed,
                "total": self.total,
            })
        if self.emit is not None:
            self.emit(EVENT_PROGRESS, {"index": self.processed, "total": self.total})

    # -- 工具 -------------------------------------------------------------
    def _apply(self, summary):
        self.success_count = summary["success"]
        self.failed_count = summary["failed"]
        self.failure_messages = list(summary["failures"])
        self.processed = summary["processed"]
        self.cancelled = summary["cancelled"]

    def _summary(self):
        return {
            "total": self.total,
            "processed": self.processed,
            "success": self.success_count,
            "failed": self.failed_count,
            "failures": list(self.failure_messages),
            "cancelled": self.cancelled,
        }

    def _emit_log(self, text):
        if self.on_log is not None:
            self.on_log(text)


class _CancelFlag:
    """把 cancel_check 回调适配成 threading.Event 风格的只读对象。"""

    def __init__(self, cancel_check):
        self._cancel_check = cancel_check

    def is_set(self):
        return bool(self._cancel_check())


class BatchWorker:
    """后台线程封装：所有事件通过 emit 回调抛出，由界面转成 queue.Queue。

    红线 3 的另一半：Tkinter 控件只在主线程操作，本类不 import tkinter。
    input_path 既可以是文件夹路径，也可以是文件列表（由 process_images 分流）。
    """

    def __init__(self, input_path, selected_format, quality, emit):
        self.input_path = input_path
        self.selected_format = selected_format
        self.quality = quality
        self.emit = emit
        self._cancel_event = threading.Event()
        self._thread = None
        self.thread_name = "imagetool-worker"
        self._total_cache = None

    # -- 外部控制 ---------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
        self._thread.start()
        return self._thread

    def request_cancel(self):
        """请求取消：只置标志位，等当前图片处理完自然生效。"""
        self._cancel_event.set()

    def is_cancelled(self):
        return self._cancel_event.is_set()

    def get_total(self):
        """进度基准：待处理图片数量（文件夹模式数文件，文件列表模式取长度）。"""
        if self._total_cache is None:
            self._total_cache = self._count_pending_images()
        return self._total_cache

    def _count_pending_images(self):
        if isinstance(self.input_path, list):
            return len(filter_existing_images(self.input_path))
        return get_input_image_total(self.input_path)

    # -- 线程主体 ---------------------------------------------------------
    def _run(self):
        try:
            total = self.get_total()
            if total == 0:
                self._emit(EVENT_LOG, "没有找到需要处理的 JPG/PNG 图片，任务结束。")
                self._emit(EVENT_PROGRESS, {"index": 0, "total": 0})
                self._emit(EVENT_FINISHED, {"total": 0, "processed": 0, "success": 0, "failed": 0})
                return

            # 统一进度基准：最大值就是待处理图片数量
            self._emit(EVENT_PROGRESS, {"index": 0, "total": total})

            # 文件列表模式：先过滤掉已经被删除/不是图片的条目，避免无谓失败
            input_path = self.input_path
            if isinstance(input_path, list):
                input_path = filter_existing_images(input_path)

            summary = process_images(
                input_path,
                quality=self.quality,
                format=self.selected_format,
                callback=self._on_callback,
                cancel_event=self._cancel_event,
            )
            if summary["cancelled"]:
                self._emit(EVENT_LOG, "任务已取消，已完成的图片不受影响。")
                self._emit(EVENT_CANCELLED, {"total": summary["total"]})
                return
            self._emit(EVENT_FINISHED, summary)
        except SubfolderCreationError as exc:
            # 红线 8：无法创建子文件夹 -> 中文弹窗提示并终止任务
            self._emit(EVENT_FOLDER_ERROR, {"message": shorten_message(exc, 220)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死掉
            detail = shorten_message(exc)
            self._emit(EVENT_LOG, "任务异常结束：{0}".format(detail))
            self._emit(EVENT_FINISHED, {
                "total": 0, "processed": 0, "success": 0, "failed": 0, "error": detail,
            })

    # -- 事件转发 ---------------------------------------------------------
    def _on_callback(self, payload):
        if "log" in payload:
            self._emit(EVENT_LOG, payload["log"])
            return
        self._emit(EVENT_FILE_DONE, {
            "source_name": payload.get("source", ""),
            "success": bool(payload.get("success")),
            "result": payload.get("output") or payload.get("message") or "",
            "processed": int(payload.get("index", 0) or 0),
            "total": int(payload.get("total", 0) or 0),
        })
        self._emit(EVENT_PROGRESS, {
            "index": int(payload.get("index", 0) or 0),
            "total": int(payload.get("total", 0) or 0),
        })

    def _emit(self, event_type, payload):
        try:
            self.emit(event_type, payload)
        except Exception:
            # 界面已经销毁时忽略发送失败
            pass


def describe_summary(summary):
    """把统计结果转成全中文的一句话总结，供界面直接显示。"""
    total = int(summary.get("total", 0) or 0)
    success = int(summary.get("success", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    if total == 0 and success == 0 and failed == 0:
        return "没有需要处理的图片。"
    text = "全部完成：共 {0} 张，成功 {1} 张，失败 {2} 张。".format(total, success, failed)
    if summary.get("cancelled"):
        text = "已取消：共 {0} 张，已处理 {1} 张，成功 {2} 张，失败 {3} 张。".format(
            total, int(summary.get("processed", 0) or 0), success, failed
        )
        return text
    if failed > 0:
        text += " 失败原因见日志。"
    return text
