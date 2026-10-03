# -*- coding: utf-8 -*-
"""ImageTool 批量图片处理 —— Tkinter 界面入口。

线程模型（红线 3）：
    子线程（processor.BatchWorker）只通过回调把事件放进 queue.Queue，
    主线程用 root.after 定期取队列并刷新控件。
    子线程绝对不直接调用任何 Tkinter 控件方法。

输入模式（状态互斥）：
    self.input_mode 为 'folder' 或 'files'，self.input_data 存路径字符串或文件列表。
    点击任意一个选择按钮都会彻底清空另一个模式的变量，保证只处理最后一次选择。
"""

import os
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from processor import (
    EVENT_CANCELLED,
    EVENT_FILE_DONE,
    EVENT_FINISHED,
    EVENT_FOLDER_ERROR,
    EVENT_LOG,
    EVENT_PROGRESS,
    BatchWorker,
    describe_summary,
)
from utils import (
    FORMAT_CHOICES,
    FORMAT_KEEP,
    OUTPUT_SUBDIR_NAME,
    QUALITY_DEFAULT,
    clamp_quality,
    filter_existing_images,
    get_output_directory_for_input,
    list_image_files,
    make_output_directory,
)

APP_TITLE = "ImageTool 批量图片处理"
PAD = 10
POLL_INTERVAL_MS = 80

# 只允许选择受支持的图片格式（新旧格式必须在同一个筛选器内）
IMAGE_FILETYPES = [("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.gif")]

MODE_FOLDER = "folder"
MODE_FILES = "files"


class ImageToolApp:
    """主窗口。所有控件操作都发生在主线程。"""

    def __init__(self, root):
        self.root = root
        self.event_queue = queue.Queue()  # 子线程 -> 主线程 的唯一通道
        self.worker = None
        self.total_files = 0
        self.processed_files = 0
        self.success_files = 0
        self.failed_files = 0

        # ---- 输入模式互斥状态（内存中明确维护）----
        self.input_mode = None    # 'folder' 或 'files'
        self.input_data = None    # 文件夹路径字符串，或图片文件路径列表

        # ---- 质量值与输入框的双向同步（带防死循环保护）----
        # 同步流程：滑块 -> Entry 设置文本 / Entry -> 滑块 set()
        # 两个方向都会再次触发对方的回调，所以用下面两个标志把回路切断。
        self._syncing_quality = False          # 正在同步时忽略回调
        self._quality_sync_count = 0           # 同步次数统计（测试可用来验证没有死循环）
        self._last_valid_quality = QUALITY_DEFAULT  # 最近一次合法值，输入非法时回退到它

        # ---- 界面状态变量（Tkinter 变量只在主线程读写）----
        self.folder_var = tk.StringVar(value="尚未选择文件夹")
        self.selection_var = tk.StringVar(value="尚未选择输入")
        self.quality_var = tk.IntVar(value=QUALITY_DEFAULT)
        self.format_var = tk.StringVar(value=FORMAT_CHOICES[0][0])  # 默认「原格式」
        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="就绪：请先选择输入文件夹，或选择图片文件")

        self._build_ui()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("760x560")
        self.root.minsize(720, 520)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 12, "bold"), padding=8)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))

        self._build_folder_section()
        self._build_setting_section()
        self._build_action_section()
        self._build_log_section()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_folder_section(self):
        """顶部：选择输入文件夹按钮 + 选择图片文件按钮（同一行）+ 已选内容显示。

        原按钮使用 grid，因此保持 grid 布局，只在右侧新增一列，
        并把列宽权重放在最右侧的标签上——原有滑动条/下拉框/进度条的位置完全不变。
        """
        frame = ttk.LabelFrame(self.root, text="1. 选择要处理的内容")
        frame.pack(fill="x", padx=PAD, pady=(PAD, 4))

        # 第一行：两个按钮横向排列（宽度一致）
        self.choose_button = ttk.Button(
            frame, text="选择输入文件夹", width=18, command=self.on_choose_folder
        )
        self.choose_button.grid(row=0, column=0, padx=(8, 6), pady=(10, 4))

        self.choose_files_button = ttk.Button(
            frame, text="选择图片文件", width=18, command=self.on_choose_files
        )
        self.choose_files_button.grid(row=0, column=1, padx=(0, 8), pady=(10, 4))

        # 第二行：显示当前选择（文件夹路径 或 已选中 N 张图片）
        ttk.Label(frame, text="当前选择：").grid(
            row=1, column=0, padx=(8, 4), pady=(0, 10), sticky="w"
        )
        self.selection_label = ttk.Label(
            frame, textvariable=self.selection_var, anchor="w", foreground="#1a4d8f"
        )
        self.selection_label.grid(row=1, column=1, padx=(0, 8), pady=(0, 10), sticky="we")
        frame.columnconfigure(1, weight=1)

        # 保留原有的路径显示（选中文件夹时同步更新），保持在原按钮右侧的位置
        self.folder_label = ttk.Label(
            frame, textvariable=self.folder_var, anchor="w", foreground="#1a4d8f"
        )
        self.folder_label.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 10), sticky="we")

    def _build_setting_section(self):
        """中部：压缩质量滑动条 + 输出格式下拉框（位置与改动前一致）。"""
        frame = ttk.LabelFrame(self.root, text="2. 处理设置")
        frame.pack(fill="x", padx=PAD, pady=4)

        # 压缩质量（1-100，默认 80）
        ttk.Label(frame, text="压缩质量：").grid(row=0, column=0, padx=(8, 4), pady=(10, 2), sticky="w")
        self.quality_scale = ttk.Scale(
            frame,
            from_=1,
            to=100,
            orient="horizontal",
            variable=self.quality_var,
            command=self.on_quality_change,
        )
        self.quality_scale.grid(row=0, column=1, padx=4, pady=(10, 2), sticky="we")
        # 原来是只读数字标签，现在改成可手动输入数字的输入框（位置、列宽不变）
        self.quality_entry_var = tk.StringVar(value=str(QUALITY_DEFAULT))
        self.quality_entry = ttk.Entry(
            frame, textvariable=self.quality_entry_var, width=5, justify="center"
        )
        self.quality_entry.grid(row=0, column=2, padx=(4, 8), pady=(10, 2), sticky="e")
        self.quality_entry.bind("<Return>", self.on_quality_entry_commit)
        self.quality_entry.bind("<KP_Enter>", self.on_quality_entry_commit)
        self.quality_entry.bind("<FocusOut>", self.on_quality_entry_commit)
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="提示：质量值越大越清晰、文件越大；输出 PNG 时会自动换算成压缩级别。",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, padx=8, pady=(0, 6), sticky="w")

        # 输出格式（原格式 / JPEG / PNG）
        ttk.Label(frame, text="输出格式：").grid(row=2, column=0, padx=(8, 4), pady=(2, 10), sticky="w")
        self.format_box = ttk.Combobox(
            frame,
            state="readonly",
            width=16,
            values=[label for label, _value in FORMAT_CHOICES],
            textvariable=self.format_var,
        )
        self.format_box.grid(row=2, column=1, padx=4, pady=(2, 10), sticky="w")
        self.format_box.bind("<<ComboboxSelected>>", self.on_format_change)

    def _build_action_section(self):
        """底部：开始处理 + 取消 + 进度条 + 状态标签（位置与改动前一致）。"""
        frame = ttk.LabelFrame(self.root, text="3. 开始处理")
        frame.pack(fill="x", padx=PAD, pady=4)

        self.start_button = ttk.Button(
            frame, text="开始处理", style="Big.TButton", width=16, command=self.on_start
        )
        self.start_button.grid(row=0, column=0, padx=(8, 6), pady=(10, 6))

        self.cancel_button = ttk.Button(
            frame, text="取消", width=10, state="disabled", command=self.on_cancel
        )
        self.cancel_button.grid(row=0, column=1, padx=(0, 8), pady=(10, 6))

        self.progress_bar = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate", variable=self.progress_var, maximum=100
        )
        self.progress_bar.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="we")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=0)

        self.status_label = ttk.Label(
            frame, textvariable=self.status_var, anchor="w", foreground="#0b6b0b"
        )
        self.status_label.grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="we")

    def _build_log_section(self):
        """额外日志区：让用户看得到每张图的结果与失败原因。"""
        frame = ttk.LabelFrame(self.root, text="处理日志")
        frame.pack(fill="both", expand=True, padx=PAD, pady=(4, PAD))

        self.log_text = tk.Text(frame, height=7, wrap="none", state="disabled", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    # ------------------------------------------------------------------
    # 输入模式互斥管理
    # ------------------------------------------------------------------
    def set_selection(self, mode, data):
        """统一设置当前选择，并彻底清空另一种模式的变量（真正的互斥）。"""
        if mode == MODE_FOLDER:
            self.input_mode = MODE_FOLDER
            self.input_data = str(data)
            self.folder_var.set(self.input_data)
            self.selection_var.set("已选中文件夹：{0}".format(self.input_data))
        elif mode == MODE_FILES:
            self.input_mode = MODE_FILES
            self.input_data = [str(item) for item in data]
            # 互斥：文件夹模式的数据必须清空
            self.folder_var.set("尚未选择文件夹")
            self.selection_var.set("已选中 {0} 张图片".format(len(self.input_data)))
        else:
            self.input_mode = None
            self.input_data = None
            self.folder_var.set("尚未选择文件夹")
            self.selection_var.set("尚未选择输入")
        self._refresh_stage_label()

    def clear_input(self):
        """清空两种模式的全部状态。"""
        self.set_selection(None, None)

    def _refresh_stage_label(self):
        """把「当前选择」同步到状态栏，让用户清楚这一次会处理什么。"""
        if self.input_mode == MODE_FOLDER and self.input_data:
            self.status_var.set("已选择文件夹：{0}".format(self.input_data))
        elif self.input_mode == MODE_FILES and self.input_data:
            self.status_var.set("已选中 {0} 张图片".format(len(self.input_data)))
        else:
            self.status_var.set("就绪：请先选择输入文件夹，或选择图片文件")

    # ------------------------------------------------------------------
    # 界面事件回调（全部在主线程）
    # ------------------------------------------------------------------
    def on_choose_folder(self):
        """选择输入文件夹：清空已选文件列表，保证互斥。"""
        folder = filedialog.askdirectory(title="请选择要处理的图片文件夹")
        if not folder:
            return
        self.set_selection(MODE_FOLDER, folder)
        image_count = len(list_image_files(folder))
        self.status_var.set("已选择文件夹，找到 {0} 张 JPG/PNG 图片".format(image_count))
        self._append_log("已选择文件夹：{0}".format(folder))
        self._append_log("其中受支持的图片：{0} 张".format(image_count))

    def on_choose_files(self):
        """选择单张/多张图片文件：清空已选文件夹，保证互斥。"""
        selected = filedialog.askopenfilenames(
            title="请选择要处理的图片（可按住 Ctrl 或 Shift 多选）",
            filetypes=IMAGE_FILETYPES,
        )
        if not selected:
            return
        # 过滤掉不存在的条目，保证计数准确
        files = filter_existing_images(list(selected))
        if not files:
            messagebox.showwarning(
                "没有可用的图片",
                "所选文件中没有找到 JPG 或 PNG 图片，请重新选择。\n"
                "（非 JPG/PNG 文件会被自动跳过）",
            )
            return
        self.set_selection(MODE_FILES, files)
        self.folder_var.set("尚未选择文件夹")
        self.status_var.set("已选中 {0} 张图片".format(len(files)))
        self._append_log("已选择 {0} 张图片：".format(len(files)))
        for path in files:
            self._append_log("  " + os.path.basename(path))

    def on_quality_change(self, _value=None):
        """滑块回调：把滑块的值同步到右侧输入框。

        必须加防死循环保护：Entry 是通过 textvariable 绑定的，
        这里一改文本就可能再次触发输入框方向的回调，所以先立标志位再改文本。
        """
        if self._syncing_quality:
            return
        self._syncing_quality = True
        try:
            quality = clamp_quality(self.quality_var.get())
            self.quality_entry_var.set(str(quality))
            self._last_valid_quality = quality
            self._quality_sync_count += 1
        finally:
            # 无论是否出错都要复位，否则后续同步会被永久阻塞
            self._syncing_quality = False

    def on_quality_entry_commit(self, _event=None):
        """输入框回调：回车、小键盘回车或鼠标点到别处时，把输入值同步到滑块。

        输入 abc / 空 / 999 等都会被 clamp_quality 修正为最近的有效值，绝不报错。
        """
        if self._syncing_quality:
            return  # 正在由滑块写入文本，忽略这次回调，避免死循环
        self._syncing_quality = True
        try:
            quality = self._normalize_quality_input(self.quality_entry_var.get())
            self.quality_var.set(quality)
            self.quality_entry_var.set(str(quality))  # 回写修正后的值
            self._last_valid_quality = quality
            self._quality_sync_count += 1
        finally:
            self._syncing_quality = False

    def _normalize_quality_input(self, raw_value):
        """把用户输入转成 1-100 的整数。

        - 字母、空串、纯符号 -> 回退到最近一次有效值；
        - 超出范围或小数 -> 修正到最近的有效值（1 或 100，小数四舍五入）。
        """
        text = "" if raw_value is None else str(raw_value).strip()
        if not text:
            return self._last_valid_quality
        try:
            number = float(text)
        except (TypeError, ValueError):
            # 输入的是字母等无法解析的内容：回退，不抛异常
            return self._last_valid_quality
        if number != number or number in (float("inf"), float("-inf")):
            # 防御 NaN / inf 这类特殊值
            return self._last_valid_quality
        return clamp_quality(number)

    def _get_quality(self):
        """取当前应使用的质量值：以输入框内容为准，并顺手修正与回写。"""
        quality = self._normalize_quality_input(self.quality_entry_var.get())
        self._last_valid_quality = quality
        return quality

    def on_format_change(self, _event=None):
        self.status_var.set("输出格式已设置为：{0}".format(self.format_var.get()))

    def on_start(self):
        """点击「开始处理」：按当前输入模式校验后启动后台线程。"""
        if self.worker is not None:
            return  # 已有任务在跑，避免重复点击

        if self.input_mode == MODE_FILES:
            self._start_files_task()
        elif self.input_mode == MODE_FOLDER:
            self._start_folder_task()
        else:
            messagebox.showwarning(
                "请先选择输入",
                "请先点击「选择输入文件夹」或「选择图片文件」。",
            )

    def _start_folder_task(self):
        folder = self.input_data
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("请先选择文件夹", "请先点击上方按钮选择输入文件夹。")
            return
        if not list_image_files(folder):
            messagebox.showinfo(
                "没有图片",
                "所选文件夹里没有找到 JPG 或 PNG 图片，请换一个文件夹。\n"
                "（非 JPG/PNG 文件会被自动跳过）",
            )
            return
        self._launch_worker(folder, "输入模式：文件夹（{0}）".format(folder))

    def _start_files_task(self):
        files = list(self.input_data or [])
        if not files:
            messagebox.showwarning("请先选择图片", "还没有选择任何图片文件。")
            return
        missing = [path for path in files if not os.path.isfile(path)]
        if missing:
            # 选好之后文件被移动或删除的情况，给出中文提示而不是让它变成失败记录
            files = [path for path in files if os.path.isfile(path)]
            if not files:
                messagebox.showwarning(
                    "文件不可用", "所选的图片文件都不存在了，请重新选择。"
                )
                self.clear_input()
                return
            self.set_selection(MODE_FILES, files)
            messagebox.showinfo(
                "部分文件已跳过",
                "有 {0} 个文件已不存在，已自动跳过，继续处理剩余 {1} 张。".format(
                    len(missing), len(files)
                ),
            )
        self._launch_worker(
            files, "输入模式：图片文件（{0} 张）".format(len(files))
        )

    def _launch_worker(self, input_path, mode_text):
        """按给定输入启动后台任务（两种模式共用）。"""
        quality = self._get_quality()  # 以输入框内容为准（会自动修正非法输入）
        if self.quality_var.get() != quality:
            self.quality_var.set(quality)
        if self.quality_entry_var.get() != str(quality):
            self.quality_entry_var.set(str(quality))
        selected_format = self._get_selected_format()

        # 进度基准：文件列表模式下最大值 = 列表长度（动态设置）
        total = self._count_pending(input_path)
        self.total_files = total
        self.processed_files = 0
        self.success_files = 0
        self.failed_files = 0
        self.progress_bar.configure(maximum=max(1, total))
        self.progress_var.set(0)

        output_dir = self._output_dir_for(input_path)
        self._append_log("=" * 40)
        self._append_log("开始任务：{0}，格式={1}，质量={2}".format(
            mode_text, self.format_var.get(), quality
        ))
        if output_dir:
            self._append_log("输出目录：{0}".format(output_dir))

        self._set_running(True)
        self.status_var.set("正在处理 {0} 张图片，请稍候……".format(total))

        self.worker = BatchWorker(
            input_path=input_path,
            selected_format=selected_format,
            quality=quality,
            emit=self._enqueue_event,
        )
        self.worker.start()
        self.root.after(POLL_INTERVAL_MS, self._poll_events)

    def _count_pending(self, input_path):
        if isinstance(input_path, list):
            return len(filter_existing_images(input_path))
        return len(list_image_files(input_path))

    def _output_dir_for(self, input_path):
        """仅用于日志展示：基准目录 + Compressed 子目录名。"""
        base_dir = get_output_directory_for_input(input_path)
        if not base_dir:
            return ""
        return os.path.join(str(base_dir), OUTPUT_SUBDIR_NAME)

    def on_cancel(self):
        """点击「取消」：只置标志位，等当前图片处理完才真正停止。"""
        if self.worker is None:
            return
        self.worker.request_cancel()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("已请求取消：正在等待当前图片处理完成……")
        self._append_log("收到取消请求：当前图片处理完成后停止（不会强行中断）。")

    def on_close(self):
        """关闭窗口：先请求取消并等线程收尾，避免后台线程访问已销毁的控件。"""
        if self.worker is not None:
            self.worker.request_cancel()
            thread = self.worker._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self.root.destroy()

    # ------------------------------------------------------------------
    # 子线程 -> 主线程 的事件通道
    # ------------------------------------------------------------------
    def _enqueue_event(self, event_type, payload):
        """子线程调用：只是往队列里放数据，不碰任何控件。"""
        try:
            self.event_queue.put((event_type, payload))
        except Exception:
            pass

    def _poll_events(self):
        """主线程定期执行：把队列里的事件取出来更新界面。"""
        try:
            while True:
                event_type, payload = self.event_queue.get_nowait()
                self._handle_event(event_type, payload)
        except queue.Empty:
            pass
        # 任务仍在运行时继续轮询
        if self.worker is not None:
            self.root.after(POLL_INTERVAL_MS, self._poll_events)

    def _handle_event(self, event_type, payload):
        if event_type == EVENT_LOG:
            self._append_log(payload)
        elif event_type == EVENT_PROGRESS:
            self._on_progress(payload)
        elif event_type == EVENT_FILE_DONE:
            self._on_file_done(payload)
        elif event_type == EVENT_FOLDER_ERROR:
            self._on_folder_error(payload)
        elif event_type == EVENT_FINISHED:
            self._on_finished(payload)
        elif event_type == EVENT_CANCELLED:
            self._on_cancelled(payload)

    def _on_progress(self, payload):
        """统一进度基准：total 来自处理器，进度值按当前模式换算。"""
        total = int(payload.get("total", 0) or 0)
        if total > 0:
            self.total_files = total
            self.progress_bar.configure(maximum=total)
        index = int(payload.get("index", payload.get("processed", 0)) or 0)
        if index:
            self.processed_files = index
            self.progress_var.set(index)
            self.status_var.set("进度 {0}/{1}：成功 {2}，失败 {3}".format(
                index, self.total_files, self.success_files, self.failed_files
            ))

    def _on_file_done(self, payload):
        total = int(payload.get("total", 0) or 0)
        if total > 0:
            self.total_files = total
            self.progress_bar.configure(maximum=total)
        self.processed_files = int(payload.get("processed", 0) or 0)
        self.progress_var.set(self.processed_files)
        if payload.get("success"):
            self.success_files += 1
        else:
            self.failed_files += 1
        self.status_var.set("进度 {0}/{1}：成功 {2}，失败 {3}".format(
            self.processed_files, self.total_files, self.success_files, self.failed_files
        ))

    def _on_folder_error(self, payload):
        """红线 8：创建子文件夹失败 -> 中文弹窗 + 终止任务。"""
        message = payload.get("message") or "无法在所选文件夹创建子文件夹，请换一个文件夹"
        self._append_log("错误：{0}".format(message))
        self.status_var.set("任务已终止：无法创建输出子文件夹")
        self._set_running(False)
        self.worker = None
        messagebox.showerror("无法创建子文件夹", message)

    def _on_finished(self, payload):
        summary_text = describe_summary(payload)
        self._append_log(summary_text)
        self.failed_files = int(payload.get("failed", self.failed_files) or 0)
        total = int(payload.get("total", 0) or 0)
        if total > 0:
            self.total_files = total
            self.progress_var.set(total)
        self.status_var.set(summary_text)
        self._set_running(False)
        self.worker = None
        if self.failed_files > 0:
            messagebox.showwarning("处理完成（有失败）", summary_text + "\n详细原因见下方日志。")
        else:
            messagebox.showinfo("处理完成", summary_text)

    def _on_cancelled(self, payload):
        text = "任务已取消：已处理 {0} 张，成功 {1} 张，失败 {2} 张。".format(
            self.processed_files, self.success_files, self.failed_files
        )
        self._append_log(text)
        self.status_var.set(text)
        self._set_running(False)
        self.worker = None
        messagebox.showinfo("已取消", text)

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _get_selected_format(self):
        """把下拉框显示的中文文本转换成内部格式常量。"""
        label = self.format_var.get()
        for choice_label, choice_value in FORMAT_CHOICES:
            if choice_label == label:
                return choice_value
        return FORMAT_KEEP

    def _set_running(self, running):
        if running:
            self.start_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")
            self.choose_button.configure(state="disabled")
            self.choose_files_button.configure(state="disabled")
            self.format_box.configure(state="disabled")
        else:
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            self.choose_button.configure(state="normal")
            self.choose_files_button.configure(state="normal")
            self.format_box.configure(state="readonly")

    def _append_log(self, text):
        if not text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(text) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def _force_utf8_console():
    """把标准输出切换成 UTF-8，避免中文提示在 GBK 控制台里报编码错。"""
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    """程序入口。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # 极少见：无图形界面环境。用中文提示而不是英文堆栈。
        _force_utf8_console()
        print("无法启动图形界面（可能当前环境没有桌面）：{0}".format(exc))
        return 1
    ImageToolApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
