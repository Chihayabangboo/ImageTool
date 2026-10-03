[English]
# ImageTool Batch Image Processing

A user-friendly desktop tool for Windows: select a folder or pick several images,
batch compress and convert formats if necessary, output to the `Compressed` subfolder.

- Supported input formats: **JPG / JPEG / PNG / BMP / WebP / TIFF / TIF / GIF**
- Technology stack: Python 3 + Tkinter (standard library) + Pillow
- Interface is fully in Chinese, error messages are fully in Chinese, no English error stacks thrown to the user
- All image processing is completed in the background thread, the interface will not freeze

---

## 1. Project Structure

```
ImageTool/
├─ main.py                      # Interface entry (Tkinter): responsible for layout, event queue, progress display
├─ processor.py                 # Core business logic: reading images, converting formats, saving, batch traversal, background thread
├─ utils.py                     # Utility functions: format judgment, path deduplication, parameter mapping, Chinese error wrapping
├─ selfcheck.py                 # One-click self-check script (does not depend on pytest, can be run directly)
├─ tests/                       # Test directory
│  ├─ conftest.py               # Add project root directory to sys.path
│  ├─ test_utils.py             # Pure logic tests: format judgment, path processing, quality mapping
│  ├─ test_processor_logic.py   # Pure logic tests: parameter mapping, cancellation granularity, thread event flow
│  └─ test_image_ops.py         # Tests dependent on Pillow: white background, EXIF, duplicate names, file writing
├─ requirements.txt             # Dependency list (only Pillow)
├─ pytest.ini                   # pytest configuration
├─ run.bat                      # Double-click to start (automatically select Python and check Pillow)
└─ README.md
```

Module dependencies are unidirectional, facilitating independent testing:

```
main.py  ->  processor.py  ->  utils.py
```

`utils.py` and `processor.py` **do not import tkinter**, so testing can be done in an environment without a graphical interface.

---

## 2. Installation and Running

1. Install dependencies (skip this step if Pillow is already installed on the local machine)(If you directly downloaded the .exe file from the Release, you can skip the first step.):

   ```bat
   python -m pip install -r requirements.txt
   ```

2. Choose either method to start:

   -Double-click the .exe file downloaded from Release
   - Double-click `run.bat`
   - Or execute in the project directory: `python main.py`

   Requires Python 3.8 or above (Tkinter comes bundled with the official installer).

---

## 3. Interface Description

Window title: **ImageTool Batch Image Processing**

| Area | Control | Description |
| --- | --- | --- |
| Top | 「Select Input Folder」button | Select the folder where the images to be processed are located |
| Top | 「Select Image Files」button | Select one or multiple images (Ctrl / Shift for multiple selections, the filter lists all supported formats simultaneously) |
| Top | 「Current Selection」label | Displays the folder path or 「Selected N images」 |
| Middle | 「Compression Quality」slider + numeric input box | Range 1–100, default **80**; drag the slider or directly enter a number and press Enter / click elsewhere |
| Middle | 「Output Format」drop-down box | Original format / JPEG / PNG |
| Bottom | 「Start Processing」large button | Start batch processing |
| Bottom | 「Cancel」button | Request cancellation (see below for cancellation granularity description) |
| Bottom | Progress bar + status label | Shows `Progress x/y: Success n, Failure m` |
| Bottom | Processing log | Results and failure reasons for each image, failure messages are in Chinese |

Output location:

- **Folder mode**: `Selected folder\Compressed\`
- **Image file mode**: `Folder of the first selected image\Compressed\`

Both the `Compressed` subfolders under both modes will be automatically created.

### Mutually exclusive input modes (important)

The interface maintains **only one** current input in memory:

| Variable | Value | Meaning |
| --- | --- | --- |
| `self.input_mode` | `'folder'` / `'files'` | Current input type |
| `self.input_data` | String / List | Folder path, or list of image file paths |

Clicking 「Select Image Files」will clear the folder variable, clicking 「Select Input Folder」will clear the file list variable,
**only the last selection will be processed** (`ImageToolApp.set_selection()` is the only entry). The status bar displays 「Selected N images」 or folder path accordingly.

---

## 4. Core Logic Explanation (How 9 Hard Rules Are Implemented)

### 1. Transparent Image to JPEG: Never Directly `convert('RGB')`

In `processor.normalize_image_mode()`, if an image with a transparent channel must be saved as JPEG, it must follow the white background process:

```python
rgba_image = image if mode in ("RGBA", "LA", "PA") else image.convert("RGBA")
white_background = _new_white_rgb(rgba_image.size)          # New pure white RGB image
if rgba_image.mode == "LA":
    rgba_image = rgba_image.convert("RGBA")
white_background.paste(rgba_image, mask=rgba_image.split()[-1])   # Original image as mask pasted
```

Transparent areas remain white, not black. If the image is already without a transparent channel `RGB` / `L`, the original object is returned without any extra copies.

**However, when 「Output Format = JPEG」and the image truly has a transparent channel**, the tool will not secretly fill the transparent area with white background, but instead output as PNG and explain in the log:

> Note: b.png (this image has a transparent channel and cannot be saved as JPEG, output as PNG to preserve transparency.)

The judgment entry is `processor.has_transparency()`: `RGBA` / `LA` / `PA` / `CMYKA` as well as palette images with transparency information are considered transparent images (note that you cannot write a fixed judgment `mode[3] == 'A'`, otherwise `LA` this two-character mode will be missed).

### 2. Renaming and Overwriting: Automatically Add `(1)`, `(2)` Suffix

`utils.build_output_path()` first attempts the original name, if the file already exists, it calls `utils.find_next_available_index()` to scan the directory for existing `Name(N).Extension`, starting from **the maximum sequence number + 1** to continue incrementing. Note that here you cannot just count the number of files: if the user manually deletes files, counting by number will hit existing files. The original file will never be overwritten.

### 3. Cancellation Granularity: Check Only Between Each Image

`processor.BatchProcessor.run()` checks for cancellation first in the loop body;
the "Cancel" on the interface is just `threading.Event.set()` (`BatchWorker.request_cancel()`).
So the large image being processed will finish first, it will not forcefully interrupt Pillow, nor will it leave damaged half-finished files.

### 4. Exceptions and Fault Tolerance: Skip Non-Images, Bad Images Do Not Interrupt the Queue

- `utils.list_image_files()` only collects supported extensions (`.jpg/.jpeg/.png/.bmp/.webp/.tiff/.tif/.gif`, case insensitive), subfolders and non-image files are skipped. The correspondence between extensions and formats is concentrated in one table `utils.EXTENSION_TO_FORMAT`, adding new formats only requires modifying this one place.
- `utils.filter_existing_images()` will eliminate files that were deleted after selection to avoid unnecessary failure records.
- `processor.process_one_image()` internally wraps all processing steps in a `try/except`, returning `(False, Chinese error message, "")` when failed, recorded by the batch logic and continues to the next image.
  Successfully returns `(True, output path, prompt message)`, the third element explains "why the output format is different from the selected one".
- More structured writing can be seen in `processor.handle_one_image()`, returning a `{"success", "result", "note"}` dictionary.

### 4.1 Unified Input Entry `process_images`

```python
processor.process_images(input_path, quality, format, callback, cancel_event)
```

- Use **`isinstance(input_path, list)`** to分流: If it's a list, process each file individually; otherwise, treat it as a folder path to traverse (`processor.resolve_input_files()` is the only judgment point);
- Progress baseline unified as "number of images to be processed": In list mode, the progress bar's maximum value is the **list length**,
  `callback` callback `{"index", "total", "source", "output", "success", "message"}` per image,
  the interface dynamically sets `progress_bar`'s `maximum` to this length and smoothly advances;
- `cancel_event` only checks between each image gap, not
————————————————————————————————————————————————————————————————————————————————————————————————————————
[中文]
# ImageTool 批量图片处理

一个面向普通用户（小白友好）的 Windows 桌面小工具：选择一个文件夹或挑几张图片，
一键批量压缩、必要时转换格式，输出到 `Compressed` 子文件夹。

- 支持输入格式：**JPG / JPEG / PNG / BMP / WebP / TIFF / TIF / GIF**
- 技术栈：Python 3 + Tkinter（标准库）+ Pillow
- 界面全中文，错误提示全中文，不向用户抛出英文堆栈
- 所有图片处理都在后台线程完成，界面不会卡死

---

## 一、项目结构

```
ImageTool/
├─ main.py                      # 界面入口（Tkinter）：负责布局、事件队列、进度显示
├─ processor.py                 # 核心业务逻辑：读图、转格式、保存、批量遍历、后台线程
├─ utils.py                     # 工具函数：格式判断、路径去重、参数映射、中文错误包装
├─ selfcheck.py                 # 一键自检脚本（不依赖 pytest，可直接运行）
├─ tests/                       # 测试目录
│  ├─ conftest.py               # 把项目根目录加入 sys.path
│  ├─ test_utils.py             # 纯逻辑测试：格式判断、路径处理、质量映射
│  ├─ test_processor_logic.py   # 纯逻辑测试：参数映射、取消粒度、线程事件流
│  └─ test_image_ops.py         # 依赖 Pillow 的测试：白底、EXIF、重名、文件写入
├─ requirements.txt             # 依赖清单（只有 Pillow）
├─ pytest.ini                   # pytest 配置
├─ run.bat                      # 双击启动（自动选择 python 并检查 Pillow）
└─ README.md
```

模块依赖方向是单向的，方便单独测试：

```
main.py  ->  processor.py  ->  utils.py
```

`utils.py` 与 `processor.py` **不导入 tkinter**，所以可以在没有图形界面的环境下测试。

---

## 二、安装与运行

1. 安装依赖（本机已装 Pillow 的话可以跳过）(若直接下载了Release中的.exe文件无需第一步)：

   ```bat
   python -m pip install -r requirements.txt
   ```

2. 启动方式任选其一：
  
   -双击在Release下载的.exe文件 
   - 双击 `run.bat`
   - 或者在项目目录执行：`python main.py`

   要求 Python 3.8 及以上版本（Tkinter 随官方安装包自带）。

---

## 三、界面说明

窗口标题：**ImageTool 批量图片处理**

| 区域 | 控件 | 说明 |
| --- | --- | --- |
| 顶部 | 「选择输入文件夹」按钮 | 选择要处理的图片所在文件夹 |
| 顶部 | 「选择图片文件」按钮 | 选择单张或多张图片（Ctrl / Shift 多选，筛选器同时列出全部受支持格式） |
| 顶部 | 「当前选择」标签 | 显示文件夹路径，或「已选中 N 张图片」 |
| 中部 | 「压缩质量」滑动条 + 数值输入框 | 范围 1–100，默认 **80**；可拖动滑块，也可直接输入数字后回车 / 点别处 |
| 中部 | 「输出格式」下拉框 | 原格式 / JPEG / PNG |
| 底部 | 「开始处理」大按钮 | 开始批量处理 |
| 底部 | 「取消」按钮 | 请求取消（见下文取消粒度说明） |
| 底部 | 进度条 + 状态标签 | 显示 `进度 x/y：成功 n，失败 m` |
| 底部 | 处理日志 | 每张图片的结果与失败原因，失败信息为中文 |

输出位置：

- **文件夹模式**：`所选文件夹\Compressed\`
- **图片文件模式**：`第一张所选图片所在文件夹\Compressed\`

两种模式下的 `Compressed` 子文件夹都会自动创建。

### 输入模式互斥（重要）

界面在内存中只维护**一份**当前输入：

| 变量 | 取值 | 含义 |
| --- | --- | --- |
| `self.input_mode` | `'folder'` / `'files'` | 当前输入类型 |
| `self.input_data` | 字符串 / 列表 | 文件夹路径，或图片文件路径列表 |

点击「选择图片文件」会清空文件夹变量，点击「选择输入文件夹」会清空文件列表变量，
**只有最后一次选择会被处理**（`ImageToolApp.set_selection()` 是唯一入口）。
状态栏同步显示「已选中 N 张图片」或文件夹路径。

---

## 四、核心逻辑说明（9 条硬性规则如何实现）

### 1. 透明图转 JPEG：绝不直接 `convert('RGB')`

`processor.normalize_image_mode()` 中，一旦确实要把带透明通道的图保存为 JPEG，就必须走铺白底流程：

```python
rgba_image = image if mode in ("RGBA", "LA", "PA") else image.convert("RGBA")
white_background = _new_white_rgb(rgba_image.size)          # 纯白 RGB 新图
if rgba_image.mode == "LA":
    rgba_image = rgba_image.convert("RGBA")
white_background.paste(rgba_image, mask=rgba_image.split()[-1])   # 原图作为遮罩粘贴
```

透明区域保留为白色，而不是变成黑色。已经是不带透明通道的 `RGB` / `L` 图时直接返回
原对象，不做多余拷贝。

**但「输出格式 = JPEG」时若图片真的带透明通道**，工具不会偷偷把透明区域抹成白底，
而是保留原样输出 PNG，并在日志里说明原因：

> 说明：b.png（该图片带透明通道，无法保存为 JPEG，已按 PNG 输出以保留透明区域。）

判定入口是 `processor.has_transparency()`：`RGBA` / `LA` / `PA` / `CMYKA` 以及
带 `transparency` 信息的调色板图都算透明图（注意不能写死判断 `mode[3] == 'A'`，
否则 `LA` 这种两字符模式会被漏判）。

### 2. 重名与覆盖：自动加 `(1)`、`(2)` 后缀

`utils.build_output_path()` 先尝试原名，若文件已存在则调用
`utils.find_next_available_index()` 扫描目录里已有的 `名称(N).扩展名`，从**最大序号 + 1**
继续递增。注意这里不能只数文件个数：如果用户手动删过文件，按数量推算会命中已存在的文件。
原文件永远不会被覆盖。

### 3. 取消粒度：只在每张图片之间检查

`processor.BatchProcessor.run()` 的循环体第一件事才是 `cancel_check()`；
界面上的「取消」只是 `threading.Event.set()`（`BatchWorker.request_cancel()`）。
所以正在处理的大图会先处理完，不会强行中断 Pillow，也不会留下损坏的半成品文件。

### 4. 异常与容错：非图片跳过，坏图不中断队列

- `utils.list_image_files()` 只收集受支持的扩展名（`.jpg/.jpeg/.png/.bmp/.webp/.tiff/.tif/.gif`，
  大小写不敏感），子文件夹与非图片文件跳过。扩展名与格式的对应关系集中在
  `utils.EXTENSION_TO_FORMAT` 一张表里，新增格式只需改这一处。
- `utils.filter_existing_images()` 会剔除「选好之后又被删除」的文件，避免产生无谓的失败记录。
- `processor.process_one_image()` 内部 `try/except` 包住全部处理步骤，失败时返回
  `(False, 中文错误信息, "")`，由批量逻辑记入日志并继续下一张。
  成功时返回 `(True, 输出路径, 提示信息)`，第三个元素用于解释「为什么输出格式与选择不同」。
- 更结构化的写法见 `processor.handle_one_image()`，返回
  `{"success", "result", "note"}` 字典。

### 4.1 统一输入入口 `process_images`

```python
processor.process_images(input_path, quality, format, callback, cancel_event)
```

- 用 **`isinstance(input_path, list)`** 分流：是列表就按文件列表逐张处理，
  否则当作文件夹路径遍历（`processor.resolve_input_files()` 是唯一的判定点）；
- 进度基准统一为「待处理图片数量」：文件列表模式下进度条最大值就是**列表长度**，
  `callback` 逐张回调 `{"index", "total", "source", "output", "success", "message"}`，
  界面据此把 `progress_bar` 的 `maximum` 动态设置为该长度并平滑推进；
- `cancel_event` 只在每张图片之间的间隙检查，不强行中断 Pillow；
- 无法创建输出子文件夹时抛出 `SubfolderCreationError`，由界面弹中文提示。
- 返回 `{"total", "processed", "success", "failed", "failures", "cancelled"}`。

### 5. 错误提示全中文

`utils.format_error_message()` 把任何异常（包括 Pillow 的英文报错）包装成
`处理失败：文件名（原因：……）`，并压成单行、限制长度，绝不会把 Traceback 展示给用户。

### 6. 参数映射依据「最终输出格式」

`processor.build_save_options()` 只看**用户选择或降级后的目标格式**，绝不看原图格式：

| 输出格式 | 保存参数 |
| --- | --- |
| JPEG | `quality`（1–100），质量 ≥ 60 时附加 `optimize=True` |
| WebP | `quality`（1–100，有损模式） |
| PNG | `compress_level`（由 1–100 线性映射到 0–9），**不传 `quality`** |
| GIF | 只给 `optimize`，**忽略 quality**（GIF 调色板格式不支持它） |
| TIFF | 默认无损 `compression="tiff_lzw"`；质量 ≥ 90 时用 JPEG 压缩，且质量上限 95 |

映射函数为 `utils.quality_to_compress_level()`，例如质量 80 → 级别 7，质量 100 → 级别 9。

### 6.1 多帧图片只处理第一帧

Pillow 打开 GIF / TIFF 后可能停在中途帧，因此处理前会显式 `seek(0)`，
并在日志里给出一条中文提示：

> 说明：loop.gif（检测到多帧图片，仅处理第一帧）

同一文件**只提示一次**（`processor.seek_first_frame()` 用去重集合记录），批量循环不会刷屏。

> 实现坑点：`ImageOps.exif_transpose()` 会返回**新对象**且新对象没有 `n_frames` 属性，
> 所以帧数必须在转置之前读出（见 `load_image_for_processing()`），否则多帧永远检测不到。

### 6.2 特殊格式保存降级

当输出为「原格式」且原图是 WebP / TIFF / GIF，某些环境缺少对应编解码器会保存失败。
`processor.save_image_with_fallback()` 会捕获异常并降级：

1. 日志提示「保存为 .webp 失败（原因：…），已自动转为 JPEG。」；
2. 输出后缀同步改成 `.jpg`（`photo.webp` → `photo.jpg`），并复用 `(1)`、`(2)` 重名逻辑；
3. 原图带透明通道时先铺白底（复用规则 1 的遮罩粘贴逻辑），避免透明区域变黑；
4. 目标格式本来就是 JPEG / PNG 时**不降级**（编解码器一定存在，失败说明是磁盘/权限问题），
   直接按普通失败记录，避免无效重试。

### 7. EXIF 旋转校正

`processor.correct_exif_orientation()` 使用 `ImageOps.exif_transpose()`。
兼容老版本 Pillow（无该方法时原样返回）以及返回 `None` 的情况，
EXIF 损坏时也不让整张图失败。

### 8. 输入文件夹写入权限

`utils.make_output_directory()` 捕获 `OSError` 并抛出 `SubfolderCreationError`，
由 `BatchWorker` 转成 `EVENT_FOLDER_ERROR` 事件，主线程收到后：

- 弹窗提示 **「无法在所选文件夹创建子文件夹，请换一个文件夹」**
- 立即终止任务，不再处理任何图片

### 9. 测试导入异常处理

`tests/test_image_ops.py` 中 Pillow 的导入使用 `try/except` 并配合
`pytest.mark.skipif`；缺少 Pillow 时该文件所有用例显示为 **skipped**，
其余纯逻辑测试照常运行，不会因为 `ImportError` 中断整个测试过程。

---

## 五、线程模型（为什么界面不会卡）

```
子线程 BatchWorker ──emit(事件)──> queue.Queue ──root.after 轮询──> 主线程刷新控件
```

- 子线程只做图片处理，并且只调用 `queue.Queue.put()` 投递事件；
- 主线程用 `root.after(80ms, self._poll_events)` 取出事件，更新进度条、状态标签、日志；
- 子线程**绝不直接操作任何 Tkinter 控件**（因此 `processor.py` 里没有 `import tkinter`）；
- 关闭窗口时先请求取消并 `join(timeout=2)`，避免后台线程访问已销毁的控件。

事件类型：`log`、`progress`、`file_done`、`folder_error`、`finished`、`cancelled`。

---

## 六、测试

### 方式一：pytest（推荐）

```bat
python -m pytest
```

- 跑单个文件：`python -m pytest tests/test_utils.py -v`
- 打印每一步：`python -m pytest -v -s`
- 输出很长时只看最后 30 行：`python -m pytest 2>&1 | Select-Object -Last 30`

### 方式二：一键自检（无需 pytest）

```bat
python selfcheck.py
```

### 测试覆盖内容

**纯逻辑测试（不需要 Pillow）**

| 测试项 | 说明 |
| --- | --- |
| 图片格式判断 | `.jpg/.jpeg/.png/.bmp/.webp/.tiff/.tif/.gif` 通过；`.txt/.psd/.raw/.heic/.svg`、无扩展名、空路径全部跳过 |
| 扩展名表一致性 | 扩展名表、输出扩展名、界面筛选器三者互相自洽（`.tif` 也有映射） |
| 输入类型判定 | `is_image_file_list()`：列表为真，字符串/元组/None 为假 |
| 输入分流 | `resolve_input_files()`：列表原样返回，文件夹则遍历；空列表不被当成当前目录 |
| 进度基准 | 文件列表模式 `total` 恒等于列表长度，`index` 从 1 平滑到 N；文件夹模式同样只数图片 |
| worker 进度事件 | `EVENT_PROGRESS` 先报 `index=0/total=N`，再逐张推进，供界面设置 `maximum` |
| 删除容错 | 选好之后被删除的文件不计入基准、不产生失败记录 |
| 模式互斥 | 先文件夹后列表（及反向）只处理最后一次选择；界面 `input_mode` / `input_data` 同步清空 |
| 界面状态 | 「已选中 N 张图片」文案、按钮同宽同行、`filetypes` 只列图片 |
| 扩展名归一化 | `.JPG`、`.PnG`、无扩展名等边界情况 |
| 原格式解析 | 「原格式」时按扩展名决定格式（含 BMP/WebP/TIFF/GIF）；不认识则返回 `None` |
| 输出路径生成 | 输出到 `Compressed`；PNG 选择时扩展名换成 `.png`；**不会出现 `Compressed\Compressed` 嵌套** |
| 重名去重 | 已有 `photo.jpg` → 生成 `photo(1).jpg`；已有 `photo(3).jpg` → 生成 `photo(4).jpg`；原文件名本身带 `(1)` 也能正确追加 |
| 文件夹列出 | 只列受支持的图片、忽略子文件夹与其它类型、按名称排序 |
| 多帧处理 | 断言对多帧对象调用了 `seek(0)`；单帧不动；同一文件的多帧提示只出现一次；`seek` 抛错不崩溃 |
| 保存降级 | WebP 保存失败 → 输出 `photo.jpg`；透明图先铺白底（四角 ≥235）；复用 `(1)` 后缀；JPEG/PNG 失败不降级 |
| 子文件夹创建失败 | 中文异常信息包含「无法在所选文件夹创建子文件夹，请换一个文件夹」 |
| 质量映射 | 1/5/6/50/80/95/100 → 0/0/1/5/7/9/9；越界值与非法值被限制或回退 |
| 保存参数 | 按最终输出格式决定：JPEG/WebP 给 `quality`，PNG 给 `compress_level`，GIF 忽略 quality，TIFF 走无损 |
| 透明判断 | RGBA/LA/PA/RGBa/CMYKA 与带透明色的 P 图判定为透明 |
| 中文错误 | 失败信息以「处理失败：」开头、单行、不含 Traceback |
| 取消粒度 | 3 张图时取消检查恰好 3 次；取消后只处理完当前一张就停（文件夹与列表模式各有一组） |
| 线程事件流 | 后台线程发出 `progress` / `file_done` / `finished`；`folder_error` 时不继续处理；回调抛异常线程也不崩溃 |

**依赖 Pillow 的测试（缺依赖时自动 skip）**

| 测试项 | 对应红线 |
| --- | --- |
| 透明图强行转 JPEG 时四角为白色（≥235，不是黑色） | 规则 1 |
| 带透明通道的图选择 JPEG 时输出 PNG，且 alpha 保持不变 | 规则 1 |
| 输出 PNG 保留透明通道（RGBA、alpha=0） | 规则 1 / 6 |
| 端到端处理不覆盖同名文件，生成 `photo(1).jpg` | 规则 2 |
| 连续处理三次得到 `photo.jpg` / `photo(1).jpg` / `photo(2).jpg` | 规则 2 |
| 损坏的 `.jpg` 只算失败 1 张，其后的图片照常成功 | 规则 4 |
| 真实多帧 GIF / TIFF 端到端处理（生成不了则跳过） | 规则 6.1 |
| BMP / WebP / TIFF / GIF 真实文件端到端转换 | 规则 4 / 6 |
| 质量 1/50/80/100 时 PNG、JPEG 都能保存成功 | 规则 6 |
| EXIF `Orientation=6` 的 40×20 图输出为 20×40 | 规则 7 |
| 老版本 Pillow 缺少 `exif_transpose` 时原样返回 | 规则 7 |
| `Compressed` 创建失败时发出 `folder_error` 且不处理任何图片 | 规则 8 |

### 实测结果（本机 Python 3.13.15 + Pillow 12.3.0）

```
python -m pytest          ->  191 passed in 0.79s
python selfcheck.py       ->  通过 18 项，失败 0 项。自检全部通过。
```

> 说明：GUI 用例共享一个 Tk 根窗口（session 级 fixture）。若每条用例都新建/销毁根窗口，
> Tcl 解释器反复初始化会偶发 "Can't find a usable tk.tcl" 的资源耗尽错误。

质量输入框另有真实事件驱动的界面验证（`event_generate` 模拟真回车 / 真失焦）：

```
控件类型: TEntry | 初始值: 80
滑块设为 1 / 37 / 100 / 80  -> 输入框: 1 / 37 / 100 / 80     （滑块 -> 输入框 同步）
输入 45  + 回车  -> 滑块: 45  | 输入框: 45
输入 999 + 失焦  -> 滑块: 100 | 输入框: 100
输入 abc + 回车  -> 滑块: 100 | 输入框: 100                  （回退到最近有效值，未崩溃）
同步计数器: 7 | 防死循环标志: False                          （无无限回调）

界面层另有手工冒烟验证：

- 两个按钮宽度一致（18）、同一行相邻列（row=0: column 0 与 1），滑动条/下拉框/进度条位置未变；
- 模拟点击「选择图片文件」选中 2 张后：`input_mode=files`、`input_data` 为那 2 个路径、
  文件夹变量被清空、状态栏显示「已选中 2 张图片」；
- 进度条 `maximum` 动态变为 2 并推进到 2.0，处理结果 2 成功 0 失败；
- 输出仅在 `Compressed\` 下（没有 `Compressed\Compressed` 嵌套），同目录下未被选中的 `c.png` 没有被处理。

---

## 七、常见问题

**Q：处理完的文件在哪？**
A：在所选文件夹下的 `Compressed` 子文件夹里，不会覆盖你的原图。同名文件会自动加
`(1)`、`(2)` 后缀。

**Q：为什么滑动条调到 30，PNG 文件大小没啥变化？**
A：PNG 是无损格式，质量值在这里被换算成压缩级别（1–100 → 0–9），只影响压缩速度与体积，
不影响画面清晰度；想要明显的体积变化请选择输出 JPEG。

**Q：点了取消为什么没立刻停？**
A：这是刻意设计：取消只在每张图片处理完成后生效，避免强行中断图片处理产生损坏文件。
处理大图时需要等当前这张结束。

**Q：提示「无法在所选文件夹创建子文件夹，请换一个文件夹」怎么办？**
A：说明该文件夹没有写入权限（例如系统盘根目录、只读盘、受保护目录）。
换一个自己有写入权限的文件夹（如桌面、文档目录）即可。

**Q：输出格式选「JPEG」，但输入里有些 PNG 是透明背景的？**
A：这类图片会原样保留为 PNG 输出（`b.png` → `b.png`），并在日志里注明原因，
不会把透明区域抹成白底。真正需要白底 JPEG 时，请先用别的工具把透明区域铺底后再处理。

**Q：如果透明图被强行保存为 JPEG 会怎样？**
A：代码里已经做了保护（`normalize_image_mode` 必须先建白色背景再以原图作为遮罩粘贴），
不存在直接 `convert('RGB')` 导致透明区域发黑的路径；测试用例会断言四角亮度 ≥ 235。

---

**Q：双击 `run.bat` 时控制台一堆「不是内部或外部命令」乱码？**
A：这是 `.bat` 文件被保存成 UTF-8 中文导致的（cmd 按本地 ANSI 代码页读取批处理，
非 ASCII 字节会破坏引号配对，于是每行都报错）。本项目当前的 `run.bat` 已改为
**纯 ASCII、零中文**，启动提示用英文，中文提示全部由 Python 界面输出。
如你后续修改该文件，请**只使用英文字符**保存（ASCII 编码）。

**Q：质量数值输入框怎么用？**
A：拖动滑块时输入框会自动同步；也可以在输入框里直接打数字，按**回车**或用鼠标点到
别的地方即生效，滑块会跳到对应位置。输入非法内容不会报错：
`abc`、空值、纯符号会**回退到最近一次有效值**，`0` / `-5` 修正为 `1`，`999` 修正为 `100`，
小数按四舍五入（如 `79.6` → `80`）。滑块与输入框之间用标志位切断回路，不会出现
"互相触发"导致的卡死。

---

## 八、开发约定

- 注释、日志、界面文本全部使用中文；变量名与函数名使用英文。
- `run.bat` 必须保持纯 ASCII（可用 `run.bat` 旁的注释提醒自己），中文提示交给 Python 输出。
- 新增逻辑优先放进 `utils.py` / `processor.py` 并补上不依赖 Pillow 的纯逻辑测试。
- 界面代码只负责布局与事件分发，不要在 `main.py` 里写图片处理逻辑。
