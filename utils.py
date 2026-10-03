# -*- coding: utf-8 -*-
"""工具函数模块。

只存放与界面无关、可单独测试的小工具：
- 图片格式判断、路径处理
- 输出文件名去重（重名自动加 (1)、(2) 后缀）
- 质量值 -> 各种格式保存参数的映射
- 中文错误信息包装
"""

import os
import re
import sys

# 允许的输入扩展名。
# 注意：这里只描述「扩展名 -> 格式」的映射关系，格式过滤与后缀判断都以本表为唯一依据，
# 新增格式只需要在这里补一行，界面筛选器与遍历逻辑会自动同步。
JPEG_EXTENSIONS = (".jpg", ".jpeg")
PNG_EXTENSIONS = (".png",)
BMP_EXTENSIONS = (".bmp",)
WEBP_EXTENSIONS = (".webp",)
TIFF_EXTENSIONS = (".tiff", ".tif")
GIF_EXTENSIONS = (".gif",)

# 仍然保留这个常量名，方便既有调用方（界面、测试）继续使用
SUPPORTED_EXTENSIONS = (
    JPEG_EXTENSIONS + PNG_EXTENSIONS + BMP_EXTENSIONS + WEBP_EXTENSIONS
    + TIFF_EXTENSIONS + GIF_EXTENSIONS
)

# 界面「选择图片文件」对话框的筛选器（新旧格式必须在同一个筛选器内）
IMAGE_FILETYPE_PATTERN = "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.gif"

# 输出格式下拉框的取值（界面显示中文，代码里统一用英文常量）
FORMAT_KEEP = "keep"
FORMAT_JPEG = "JPEG"
FORMAT_PNG = "PNG"
FORMAT_BMP = "BMP"
FORMAT_WEBP = "WEBP"
FORMAT_TIFF = "TIFF"
FORMAT_GIF = "GIF"

# 扩展名 -> 内部格式常量（后缀判断的唯一依据）
EXTENSION_TO_FORMAT = {
    ".jpg": FORMAT_JPEG,
    ".jpeg": FORMAT_JPEG,
    ".png": FORMAT_PNG,
    ".bmp": FORMAT_BMP,
    ".webp": FORMAT_WEBP,
    ".tiff": FORMAT_TIFF,
    ".tif": FORMAT_TIFF,
    ".gif": FORMAT_GIF,
}

# 内部格式常量 -> 保存时使用的输出扩展名
FORMAT_TO_EXTENSION = {
    FORMAT_JPEG: ".jpg",
    FORMAT_PNG: ".png",
    FORMAT_BMP: ".bmp",
    FORMAT_WEBP: ".webp",
    FORMAT_TIFF: ".tiff",
    FORMAT_GIF: ".gif",
}

# 界面显示名称
FORMAT_LABELS = {
    FORMAT_JPEG: "JPEG",
    FORMAT_PNG: "PNG",
    FORMAT_BMP: "BMP",
    FORMAT_WEBP: "WebP",
    FORMAT_TIFF: "TIFF",
    FORMAT_GIF: "GIF",
}

# 多帧格式：这些格式一张文件里可能有多帧，按需求只处理第一帧
MULTI_FRAME_FORMATS = (FORMAT_TIFF, FORMAT_GIF)

# 界面「输出格式」下拉框的显示文本 -> 内部取值
FORMAT_CHOICES = (
    ("原格式", FORMAT_KEEP),
    ("JPEG", FORMAT_JPEG),
    ("PNG", FORMAT_PNG),
)

# 输出子文件夹名（固定写在输入文件夹内部）
OUTPUT_SUBDIR_NAME = "Compressed"

# 质量滑动条的取值范围
QUALITY_MIN = 1
QUALITY_MAX = 100
QUALITY_DEFAULT = 80


class SubfolderCreationError(Exception):
    """无法在输入文件夹下创建输出子文件夹时抛出（通常是权限不足或路径只读）。"""


def get_file_extension(path):
    """返回小写扩展名（含点号），例如 ".JPG" -> ".jpg"。路径为空时返回空串。"""
    if not path:
        return ""
    return os.path.splitext(str(path))[1].lower()


def is_supported_image(path):
    """判断路径是否为工具支持的图片（按扩展名判断，支持 JPG/JPEG/PNG/BMP/WebP/TIFF/GIF）。

    注意：这里不读取文件内容，所以损坏的图片也会返回 True，
    真正的损坏在处理器里由异常捕获逻辑处理。
    无扩展名、空路径、以及不在表内的扩展名（如 .txt）一律返回 False。
    """
    return get_file_extension(path) in EXTENSION_TO_FORMAT


def get_output_format_from_extension(ext):
    """根据扩展名推断 PIL 保存格式；不认识时返回 None。

    支持 .jpg/.jpeg/.png/.bmp/.webp/.tiff/.tif/.gif（大小写不敏感）。
    """
    ext = (ext or "").lower()
    return EXTENSION_TO_FORMAT.get(ext)


def resolve_target_format(file_path, selected_format):
    """解析某张图片最终要保存成什么格式。

    selected_format 为 "keep" 时沿用原扩展名对应的格式。
    返回 FORMAT_JPEG / FORMAT_PNG，无法判断时返回 None。
    """
    if selected_format in (FORMAT_JPEG, FORMAT_PNG):
        return selected_format
    return get_output_format_from_extension(get_file_extension(file_path))


def get_format_extension(target_format):
    """把内部格式常量转成输出扩展名（JPEG 统一用 .jpg）。"""
    return FORMAT_TO_EXTENSION.get(target_format, "")


def get_format_label(target_format):
    """把内部格式常量转成给用户看的中文名称。"""
    return FORMAT_LABELS.get(target_format, "原格式")


def clamp_quality(value):
    """把质量值限制在 1-100 之间；非数字时回退到默认值 80。"""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return QUALITY_DEFAULT
    return max(QUALITY_MIN, min(QUALITY_MAX, number))


def quality_to_compress_level(quality):
    """把 1-100 的质量值映射到 PNG 的 compress_level（0-9）。

    Pillow 的 quality 参数只对 JPEG 有效，PNG 必须用 compress_level，
    否则要么报错要么被静默忽略。

    这里用「四舍五入」而不是 Python 内置 round()：内置 round 是银行家舍入
    （round(4.5) == 4），会让质量 50 映射成 4 而不是中间值 5，不符合直觉。
    """
    quality = clamp_quality(quality)
    level = int((quality * 9 + 50) // 100)  # 等价于 round(quality / 100 * 9)
    return max(0, min(9, level))


def split_name_and_extension(file_name):
    """拆分文件名与扩展名，返回 (主名, 扩展名)，扩展名保留原始大小写形式。"""
    stem, ext = os.path.splitext(str(file_name))
    return stem, ext


def add_suffix_to_file_name(file_name, index):
    """给文件名加上 (1)/(2) 之类的后缀，扩展名位置保持不变。"""
    stem, ext = split_name_and_extension(file_name)
    return "{0}({1}){2}".format(stem, index, ext)


_SUFFIX_PATTERN = re.compile(r"^(?P<stem>.*)\((?P<index>\d+)\)$")


def split_suffix_from_file_name(file_name):
    """从已带后缀的文件名里拆出 (主名, 序号)。

    例如 "photo(2).jpg" -> ("photo", 2)；没有后缀时返回 (原名, None)。
    用于已存在大量历史文件时，从最大序号继续递增。
    """
    stem, _ext = split_name_and_extension(file_name)
    matched = _SUFFIX_PATTERN.match(stem)
    if not matched:
        return stem, None
    return matched.group("stem"), int(matched.group("index"))


def find_next_available_index(folder, file_name):
    """在磁盘上查找第一个不冲突的 (N) 序号。

    不能只靠“数文件个数”：如果用户手动删过文件，序号会重复命中已存在的文件。
    """
    stem, ext = split_name_and_extension(file_name)
    if not os.path.isdir(folder):
        return 1
    max_index = 0
    prefix = stem + "("
    for entry in os.listdir(folder):
        if not entry.startswith(prefix) or not entry.lower().endswith(ext.lower()):
            continue
        _base, index = split_suffix_from_file_name(entry)
        if index is not None and index > max_index:
            max_index = index
    return max_index + 1


def build_output_path(output_dir, source_path, target_format):
    """生成输出文件路径：绝不覆盖原文件，重名时自动添加 (1)、(2) 后缀。"""
    file_name = os.path.basename(str(source_path))
    stem, _ext = split_name_and_extension(file_name)
    new_name = stem + get_format_extension(target_format)
    candidate = os.path.join(output_dir, new_name)
    if not os.path.exists(candidate):
        return candidate
    index = find_next_available_index(output_dir, new_name)
    while True:
        candidate = os.path.join(output_dir, add_suffix_to_file_name(new_name, index))
        if not os.path.exists(candidate):
            return candidate
        index += 1


def make_output_directory(parent_dir):
    """在父目录下创建 Compressed 子文件夹。

    创建失败（权限不足、目录只读、路径过长等）时抛出 SubfolderCreationError，
    由调用方弹出中文提示并终止任务。
    """
    output_dir = os.path.join(str(parent_dir), OUTPUT_SUBDIR_NAME)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise SubfolderCreationError(
            "无法在所选文件夹创建子文件夹，请换一个文件夹（原因：{0}）".format(exc)
        )
    if not os.path.isdir(output_dir):
        raise SubfolderCreationError("无法在所选文件夹创建子文件夹，请换一个文件夹")
    return output_dir


def is_image_file_list(input_path):
    """判断输入是「图片文件列表」还是「文件夹路径」。

    只要传入的是 list（含 list 的子类）就视为文件列表，其它一律当文件夹路径。
    界面与 processor 都用这一个函数判定，避免两处规则不一致。
    """
    return isinstance(input_path, list)


def filter_existing_images(file_list, check_extension=True):
    """过滤出仍然存在的图片文件。

    用户在多选之后可能又删掉了某些文件，或选中了非图片文件；
    这里统一剔除，避免后面无谓的失败记录。
    """
    result = []
    if not file_list:
        return result
    for item in file_list:
        if item is None:
            continue
        path = str(item)
        if check_extension and not is_supported_image(path):
            continue
        try:
            if not os.path.isfile(path):
                continue
        except OSError:
            continue
        result.append(path)
    return result


def get_output_directory_for_input(input_path):
    """取得输出目录的「基准文件夹」。

    - 文件夹模式：返回该文件夹路径；
    - 文件列表模式：返回列表中第一张图片所在文件夹（方便用户找到结果）；
    - 无法判断时返回 None。
    """
    if is_image_file_list(input_path):
        for item in input_path:
            if item is None:
                continue
            parent = os.path.dirname(os.path.abspath(str(item)))
            if parent:
                return parent
        return None
    if not input_path:
        return None
    return str(input_path)


def list_image_files(folder):
    """列出文件夹内（不递归）所有受支持的图片文件名，按名称排序保证结果稳定。

    读取目录失败（权限不足、路径失效、文件名编码异常等）时返回空列表，
    由调用方按「没有找到图片」处理，避免把英文异常弹给用户。
    """
    if not folder or not os.path.isdir(folder):
        return []
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    names = []
    for entry in entries:
        full_path = os.path.join(folder, entry)
        try:
            if not os.path.isfile(full_path):
                continue
        except OSError:
            continue
        if is_supported_image(entry):
            names.append(entry)
    names.sort()
    return names


def shorten_message(message, limit=160):
    """把异常信息压成一行、限制长度，避免日志被超长报错刷屏。"""
    text = "" if message is None else str(message)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def format_error_message(source_name, exc):
    """把任意异常包装成全中文的提示，绝不把英文堆栈抛给用户。"""
    return "处理失败：{0}（原因：{1}）".format(source_name, shorten_message(exc))


def get_app_base_dir():
    """返回程序所在目录；打包成 exe 时返回 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))
