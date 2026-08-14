#!/usr/bin/env python3
"""AI剧生产套件 - 独立桌面 GUI 工具

五标签页：
  Tab 1: MD → Excel（分镜头需求 / H3提示词 MD 转审阅表 Excel + 规范自检）
  Tab 2: 资产管理（收集/映射/上传美术资产路径）
  Tab 3: Excel → JSON（审阅表Excel转多链生产JSON）
  Tab 4: .pt 元数据读取器（扫描 .pt 缓存文件，查看时长/分辨率/提示词等）
  Tab 5: 资产准备表 → Krea2 JSON（美术资产准备表MD转Krea2工作流JSON）

Tab 1 支持三种 MD 格式（自动检测）：
  - 分镜头需求（旧格式 ### A01 + H3提示词）
  - 分镜头需求 v6（#### 镜头N + 10字段，不含 H3 提示词）
  - H3 提示词（## A01 九分节格式）
  并提供规范自检（指代不明/对白格式/画风冲突/时长一致性）与报告导出。

依赖同目录的 shot_md_to_excel.py、excel_to_multi_chain_json.py、pt_meta_reader.py、
asset_md_to_krea2_json.py。
"""

import os
import sys
import json
import threading
import queue
import subprocess
import importlib.util
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog


# ── 动态导入同目录的脚本 ─────────────────────────────────────────────

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _import_module(filename, modname):
    """从文件路径动态导入 Python 模块。

    优先从 TOOLS_DIR 查找，找不到则尝试上级 tools 子目录。
    """
    filepath = os.path.join(TOOLS_DIR, filename)
    if not os.path.exists(filepath):
        # 降级搜索上级 tools 子目录
        filepath = os.path.join(os.path.dirname(TOOLS_DIR), "tools", filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"找不到模块文件: {filename} (搜索目录: {TOOLS_DIR})"
        )

    spec = importlib.util.spec_from_file_location(modname, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── GUI 应用类 ───────────────────────────────────────────────────────

class H3ToolsApp:
    """AI剧生产套件主应用。"""

    def __init__(self, root):
        self.root = root
        self.root.title("AI剧生产套件")
        self.root.geometry("1000x750")
        self.root.minsize(900, 680)

        # 数据状态
        self.md_files = []           # Tab1: MD文件列表
        self.asset_mapping = {}      # Tab2: {"角色": {name: path}, "场景": {...}, "道具": {...}}
        self.asset_view_mode = "全局（全部集）"  # Tab2: 当前视图
        self.episode_assets = {}     # Tab2: 各集资产 {stem: {"角色": set, ...}}
        self.last_mapping_path = ""  # Tab2: 最后保存的映射路径
        self.log_queue = queue.Queue()

        # 动态导入模块
        self.md_module = None
        self.excel_module = None
        self.asset_krea2_module = None
        self._import_error = ""

        try:
            self.md_module = _import_module("shot_md_to_excel.py", "shot_md_to_excel")
        except Exception as e:
            self._import_error += f"无法加载 shot_md_to_excel.py: {e}\n"

        try:
            self.excel_module = _import_module(
                "excel_to_multi_chain_json.py", "excel_to_multi_chain_json"
            )
        except Exception as e:
            self._import_error += f"无法加载 excel_to_multi_chain_json.py: {e}\n"

        try:
            self.asset_krea2_module = _import_module(
                "asset_md_to_krea2_json.py", "asset_md_to_krea2_json"
            )
        except Exception as e:
            self._import_error += f"无法加载 asset_md_to_krea2_json.py: {e}\n"

        self._setup_style()
        self._build_ui()
        self._center_window()
        self.root.after(100, self._poll_log_queue)

        # 如果有导入错误，延迟提示
        if self._import_error:
            self.root.after(
                500,
                lambda: messagebox.showwarning("模块加载警告", self._import_error),
            )

    # ── 样式设置 ──

    def _setup_style(self):
        """配置 ttk 样式美化界面。"""
        style = ttk.Style()

        # 尝试使用更现代的主题
        available = style.theme_names()
        for theme in ("clam", "vista", "xpnative"):
            if theme in available:
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue

        # Notebook 样式
        style.configure("TNotebook", tabmargins=[8, 5, 0, 0])
        style.configure("TNotebook.Tab", padding=[16, 6],
                        font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", "#ffffff")],
                  foreground=[("selected", "#0066cc")])

        # 普通按钮
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(8, 4))

        # 标签
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel",
                        font=("Microsoft YaHei UI", 11, "bold"),
                        foreground="#333333")

        # Treeview 样式
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=24)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    # ── 窗口居中 ──

    def _center_window(self):
        """将窗口居中显示在屏幕上。"""
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ── 构建 UI ──

    def _build_ui(self):
        """构建主界面，包含四个标签页。"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Tab 1: MD → Excel
        self.tab_md = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_md, text="  MD → Excel  ")
        self._build_tab_md_to_excel(self.tab_md)

        # Tab 2: 资产管理
        self.tab_asset = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_asset, text="  资产管理  ")
        self._build_tab_asset_manager(self.tab_asset)

        # Tab 3: Excel → JSON
        self.tab_json = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_json, text="  Excel → JSON  ")
        self._build_tab_excel_to_json(self.tab_json)

        # Tab 4: .pt 元数据读取器
        self.tab_pt = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_pt, text="  .pt 元数据读取器  ")
        self._build_tab_pt_reader(self.tab_pt)

        # Tab 5: 资产准备表 → Krea2 JSON
        self.tab_krea2 = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_krea2, text="  资产准备表 → Krea2 JSON  ")
        self._build_tab_asset_krea2(self.tab_krea2)

    # ── Tab 1: MD → Excel ──

    def _build_tab_md_to_excel(self, parent):
        """构建 MD → Excel 标签页（支持分镜头需求 / H3提示词 双格式 + 规范自检）。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="MD 文件（分镜头需求 / H3提示词）")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.md_listbox = tk.Listbox(
            list_container, height=5, selectmode=tk.EXTENDED,
            font=("Consolas", 9),
        )
        md_scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                  command=self.md_listbox.yview)
        self.md_listbox.configure(yscrollcommand=md_scroll.set)
        self.md_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        md_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮行
        btn_frame = ttk.Frame(file_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(btn_frame, text="添加文件",
                   command=self._md_add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="删除选中",
                   command=self._md_remove_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="清空",
                   command=self._md_clear).pack(side=tk.LEFT, padx=4)

        # 格式选择
        fmt_frame = ttk.LabelFrame(parent, text="MD 格式")
        fmt_frame.pack(fill=tk.X, padx=10, pady=5)

        fmt_container = ttk.Frame(fmt_frame)
        fmt_container.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(fmt_container, text="格式:").pack(side=tk.LEFT, padx=(0, 4))
        self.md_format_var = tk.StringVar(value="auto")
        self.md_format_combo = ttk.Combobox(
            fmt_container, textvariable=self.md_format_var,
            state="readonly", width=30,
        )
        self.md_format_combo['values'] = [
            "自动检测（推荐）",
            "分镜头需求（旧格式 ### A01）",
            "分镜头需求 v6（#### 镜头N + 10字段）",
            "H3提示词（## A01 九分节）",
        ]
        self.md_format_combo.current(0)
        self.md_format_combo.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            fmt_container,
            text="分镜头需求与 H3 提示词已拆分，两者均可生成审阅表",
            foreground="#888888",
        ).pack(side=tk.LEFT)

        # 输出目录
        out_frame = ttk.LabelFrame(parent, text="输出目录")
        out_frame.pack(fill=tk.X, padx=10, pady=5)

        out_container = ttk.Frame(out_frame)
        out_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(out_container, text="输出目录:").pack(side=tk.LEFT)
        self.md_output_entry = ttk.Entry(out_container)
        self.md_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(out_container, text="浏览",
                   command=self._md_browse_output).pack(side=tk.LEFT)

        # 操作按钮行：开始生成 + 规范自检
        action_frame = ttk.Frame(parent)
        action_frame.pack(fill=tk.X, padx=20, pady=6)
        self.md_run_btn = tk.Button(
            action_frame, text="开始生成", height=2,
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#4a90d9", fg="white",
            activebackground="#5ba0e9", activeforeground="white",
            disabledforeground="#cccccc",
            command=self._md_start,
        )
        self.md_run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.md_check_btn = tk.Button(
            action_frame, text="规范自检", height=2,
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#d98a4a", fg="white",
            activebackground="#e9a05b", activeforeground="white",
            disabledforeground="#cccccc",
            command=self._md_check_spec,
        )
        self.md_check_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 5))
        self.md_log = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.md_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 自检报告区域
        report_frame = ttk.LabelFrame(parent, text="规范自检报告")
        report_frame.pack(fill=tk.BOTH, expand=False, padx=10, pady=(0, 10))
        report_toolbar = ttk.Frame(report_frame)
        report_toolbar.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.md_report_summary = ttk.Label(report_toolbar, text="尚未执行自检", foreground="#888888")
        self.md_report_summary.pack(side=tk.LEFT)
        ttk.Button(report_toolbar, text="导出报告(.md)",
                   command=self._md_export_report).pack(side=tk.RIGHT)
        self.md_report = scrolledtext.ScrolledText(
            report_frame, height=7, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.md_report.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._md_report_lines = []   # 最近一次自检报告行
        self._md_report_errors = 0   # 最近一次自检错误数
        self._md_report_warns = 0    # 最近一次自检警告数

    # ── Tab 2: 资产管理 ──

    def _build_tab_asset_manager(self, parent):
        """构建资产管理标签页。"""
        # 顶部行: 视图选择 + 加载/保存映射
        top_frame = ttk.Frame(parent)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        ttk.Label(top_frame, text="视图:").pack(side=tk.LEFT, padx=(0, 4))
        self.asset_view_var = tk.StringVar(value="全局（全部集）")
        self.asset_view_combo = ttk.Combobox(
            top_frame, textvariable=self.asset_view_var,
            state="readonly", width=28,
        )
        self.asset_view_combo['values'] = ["全局（全部集）"]
        self.asset_view_combo.pack(side=tk.LEFT, padx=(0, 10))
        self.asset_view_combo.bind("<<ComboboxSelected>>", self._asset_switch_view)
        ttk.Button(top_frame, text="加载映射",
                   command=self._asset_load_mapping).pack(side=tk.LEFT, padx=4)
        ttk.Button(top_frame, text="保存映射",
                   command=self._asset_save_mapping).pack(side=tk.LEFT, padx=4)

        # Treeview 资产列表
        tree_frame = ttk.LabelFrame(parent, text="资产列表")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        tree_container = ttk.Frame(tree_frame)
        tree_container.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 2))

        self.asset_tree = ttk.Treeview(
            tree_container,
            columns=("idx", "type", "name", "path"),
            show="headings", height=15,
        )
        self.asset_tree.heading("idx", text="序号")
        self.asset_tree.heading("type", text="类型")
        self.asset_tree.heading("name", text="名称")
        self.asset_tree.heading("path", text="input路径")
        self.asset_tree.column("idx", width=60, anchor=tk.CENTER)
        self.asset_tree.column("type", width=80, anchor=tk.CENTER)
        self.asset_tree.column("name", width=220, anchor=tk.W)
        self.asset_tree.column("path", width=420, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(tree_container, orient=tk.VERTICAL,
                                    command=self.asset_tree.yview)
        self.asset_tree.configure(yscrollcommand=tree_scroll.set)
        self.asset_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 操作行: 浏览/编辑 + 统计
        action_frame = ttk.Frame(tree_frame)
        action_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(action_frame, text="浏览选中行",
                   command=self._asset_browse).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(action_frame, text="手动编辑路径",
                   command=self._asset_edit_path).pack(side=tk.LEFT, padx=4)
        self.asset_stats_label = ttk.Label(
            action_frame, text="角色: 0  场景: 0  道具: 0  已填: 0/0"
        )
        self.asset_stats_label.pack(side=tk.RIGHT, padx=4)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=False, padx=10, pady=(5, 10))
        self.asset_log = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.asset_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Tab 3: Excel → JSON ──

    def _build_tab_excel_to_json(self, parent):
        """构建 Excel → JSON 标签页。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="审阅表 Excel 文件")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.json_listbox = tk.Listbox(
            list_container, height=6, selectmode=tk.EXTENDED,
            font=("Consolas", 9),
        )
        json_scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                    command=self.json_listbox.yview)
        self.json_listbox.configure(yscrollcommand=json_scroll.set)
        self.json_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        json_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮行
        btn_frame = ttk.Frame(file_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(btn_frame, text="添加文件",
                   command=self._json_add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="删除选中",
                   command=self._json_remove_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="清空",
                   command=self._json_clear).pack(side=tk.LEFT, padx=4)

        # 输出目录
        out_frame = ttk.LabelFrame(parent, text="输出目录")
        out_frame.pack(fill=tk.X, padx=10, pady=5)

        out_container = ttk.Frame(out_frame)
        out_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(out_container, text="输出目录:").pack(side=tk.LEFT)
        self.json_output_entry = ttk.Entry(out_container)
        self.json_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(out_container, text="浏览",
                   command=self._json_browse_output).pack(side=tk.LEFT)

        # 资产映射
        map_frame = ttk.LabelFrame(parent, text="资产映射")
        map_frame.pack(fill=tk.X, padx=10, pady=5)

        map_container = ttk.Frame(map_frame)
        map_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(map_container, text="资产映射:").pack(side=tk.LEFT)
        self.json_mapping_entry = ttk.Entry(map_container, state="readonly")
        self.json_mapping_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(map_container, text="浏览",
                   command=self._json_browse_mapping).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(map_container, text="使用Tab2映射",
                   command=self._json_use_tab2_mapping).pack(side=tk.LEFT)

        # 分组模式
        mode_frame = ttk.LabelFrame(parent, text="分组模式")
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        mode_container = ttk.Frame(mode_frame)
        mode_container.pack(fill=tk.X, padx=8, pady=8)
        self.json_group_mode = tk.StringVar(value="by_char")
        ttk.Radiobutton(
            mode_container, text="按角色分组(推荐)",
            variable=self.json_group_mode, value="by_char",
        ).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Radiobutton(
            mode_container, text="按镜头分组(每镜一个JSON)",
            variable=self.json_group_mode, value="by_shot",
        ).pack(side=tk.LEFT)

        # 开始按钮（tkinter default 样式, 高度2）
        self.json_run_btn = tk.Button(
            parent, text="开始生成", height=2,
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#4a90d9", fg="white",
            activebackground="#5ba0e9", activeforeground="white",
            disabledforeground="#cccccc",
            command=self._json_start,
        )
        self.json_run_btn.pack(fill=tk.X, padx=20, pady=10)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.json_log = scrolledtext.ScrolledText(
            log_frame, height=12, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.json_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Tab 1 方法 ──

    def _md_add_files(self):
        """添加 MD 文件（支持多选，分镜头需求 / H3提示词）。"""
        files = filedialog.askopenfilenames(
            title="选择分镜头需求或 H3 提示词 MD 文件",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
        )
        if not files:
            return
        existing = set(self.md_listbox.get(0, tk.END))
        for f in files:
            if f not in existing:
                self.md_listbox.insert(tk.END, f)

    def _md_get_format(self):
        """返回当前选择的格式标识: "auto"/"storyboard"/"storyboard_v6"/"h3_prompt"。"""
        idx = self.md_format_combo.current()
        return ["auto", "storyboard", "storyboard_v6", "h3_prompt"][idx] if idx >= 0 else "auto"

    def _md_resolve_format(self, md_path, forced):
        """根据强制格式或自动检测返回实际格式。"""
        if forced == "auto":
            return self.md_module.detect_format(md_path)
        return forced

    def _md_remove_selected(self):
        """删除 Listbox 中选中的项（从后往前删避免索引错乱）。"""
        selected = list(self.md_listbox.curselection())
        for i in reversed(selected):
            self.md_listbox.delete(i)

    def _md_clear(self):
        """清空 MD 文件列表。"""
        self.md_listbox.delete(0, tk.END)

    def _md_browse_output(self):
        """浏览选择输出目录。"""
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.md_output_entry.delete(0, tk.END)
            self.md_output_entry.insert(0, d)

    def _md_start(self):
        """启动 MD → Excel 处理（后台线程）。"""
        if self.md_module is None:
            messagebox.showerror("错误", "shot_md_to_excel.py 模块未加载，无法执行。")
            return

        files = list(self.md_listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 MD 文件。")
            return

        output_dir = self.md_output_entry.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return

        fmt = self._md_get_format()

        # 保存 MD 文件列表，供 Tab2 资产收集使用
        self.md_files = list(files)

        # 禁用按钮，防止重复点击
        self.md_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._md_worker, args=(files, output_dir, fmt), daemon=True,
        )
        thread.start()

    def _md_worker(self, md_files, output_dir, forced_fmt="auto"):
        """后台线程：执行 MD → Excel 转换（支持双格式 + 格式检测）。"""
        log = lambda msg: self._log("md", msg)
        success, fail = 0, 0

        try:
            log(f"=== 开始处理: {len(md_files)} 个 MD 文件 ===")
            log(f"输出目录: {output_dir}")
            log(f"格式: {forced_fmt}")
            log("")

            for i, md_path in enumerate(md_files, 1):
                fmt = self._md_resolve_format(md_path, forced_fmt)
                if not fmt:
                    log(f"[{i}/{len(md_files)}] {Path(md_path).name} -> 无法识别格式，跳过")
                    fail += 1
                    continue

                stem = Path(md_path).stem
                out_name = (stem.replace("分镜头需求", "提示词审阅表")
                                .replace("H3提示词", "提示词审阅表")) + ".xlsx"
                out_path = os.path.join(output_dir, out_name)

                log(f"[{i}/{len(md_files)}] {Path(md_path).name} ({fmt})")
                if fmt == "h3_prompt":
                    ok = self.md_module.process_h3_prompt_single(
                        md_path, out_path, log=log)
                elif fmt == "storyboard_v6":
                    ok = self.md_module.process_storyboard_v6_single(
                        md_path, out_path, log=log)
                elif fmt == "storyboard":
                    ok = self.md_module._process_storyboard_single(
                        md_path, out_path, log=log)
                else:
                    ok = self.md_module.process_single(md_path, out_path, log=log)

                if ok:
                    success += 1
                else:
                    fail += 1

            log(f"\n=== 完成: {success} 成功, {fail} 失败 ===")

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())

        finally:
            # 无论成功失败，都通知主线程收集资产并切换 Tab
            self.log_queue.put(("assets_ready",))
            self.log_queue.put(("done", "md", success, fail))

    # ── Tab 1 规范自检方法 ──

    def _md_check_spec(self):
        """对选中的 H3 提示词 MD 执行规范自检（后台线程）。"""
        if self.md_module is None:
            messagebox.showerror("错误", "shot_md_to_excel.py 模块未加载，无法执行。")
            return

        files = list(self.md_listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 MD 文件。")
            return

        # 只对 H3 提示词格式执行自检
        h3_files = []
        for f in files:
            fmt = self._md_resolve_format(f, self._md_get_format())
            if fmt == "h3_prompt":
                h3_files.append(f)
            else:
                self._log("md", f"跳过（非H3提示词格式）: {Path(f).name}")

        if not h3_files:
            messagebox.showwarning(
                "提示",
                "规范自检仅支持 H3 提示词格式（## A01 九分节）文件。\n"
                "请添加 H3 提示词 MD 文件，或检查格式选择。",
            )
            return

        self.md_check_btn.configure(state=tk.DISABLED)
        thread = threading.Thread(
            target=self._md_check_spec_worker, args=(h3_files,), daemon=True,
        )
        thread.start()

    def _md_check_spec_worker(self, h3_files):
        """后台线程：执行规范自检。"""
        log = lambda msg: self._log("md", msg)
        all_lines = []
        n_err = n_warn = 0

        try:
            log(f"=== 规范自检: {len(h3_files)} 个 H3 提示词文件 ===")
            for i, md_path in enumerate(h3_files, 1):
                log(f"[{i}/{len(h3_files)}] {Path(md_path).name}")
                report, e, w = self.md_module.spec_check_h3_prompt(md_path, log=log)
                all_lines.extend(report)
                all_lines.append("")
                n_err += e
                n_warn += w

            log(f"=== 自检完成: {n_err} 错误, {n_warn} 警告 ===")
        except Exception as e:
            log(f"\n[错误] 自检过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())
            all_lines.append(f"[错误] 自检异常: {e}")

        finally:
            self._md_report_lines = all_lines
            self._md_report_errors = n_err
            self._md_report_warns = n_warn
            self.log_queue.put(("md_report_ready",))

    def _md_export_report(self):
        """将最近一次自检报告导出为 .md 文件。"""
        if not self._md_report_lines:
            messagebox.showwarning("提示", "尚无自检报告可导出，请先执行规范自检。")
            return

        path = filedialog.asksaveasfilename(
            title="导出规范自检报告",
            defaultextension=".md",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
            initialfile="规范自检报告.md",
        )
        if not path:
            return

        try:
            self.md_module.save_spec_report(self._md_report_lines, path)
            self._log("md", f"自检报告已导出: {path}")
            messagebox.showinfo("成功", f"自检报告已导出到:\n{path}")
        except Exception as e:
            self._log("md", f"导出失败: {e}")
            messagebox.showerror("错误", f"导出失败: {e}")

    def _md_show_report(self):
        """在主线程中填充自检报告区域。"""
        self.md_report.configure(state=tk.NORMAL)
        self.md_report.delete("1.0", tk.END)
        self.md_report.insert("1.0", "\n".join(self._md_report_lines))
        self.md_report.configure(state=tk.DISABLED)

        if self._md_report_errors:
            summary = f"自检完成: {self._md_report_errors} 错误, {self._md_report_warns} 警告（需修复）"
            color = "#cc0000"
        elif self._md_report_warns:
            summary = f"自检完成: 0 错误, {self._md_report_warns} 警告（建议检查）"
            color = "#cc8800"
        else:
            summary = "自检通过: 0 错误, 0 警告"
            color = "#008800"
        self.md_report_summary.config(text=summary, foreground=color)

        self.md_check_btn.configure(state=tk.NORMAL)

    # ── Tab 2 方法 ──

    def _populate_asset_tab(self):
        """收集全局资产并填充到 Treeview（由主线程队列触发）。"""
        if not self.md_files:
            self._log("asset", "没有 MD 文件，无法收集资产。")
            return
        if self.md_module is None:
            self._log("asset", "shot_md_to_excel.py 模块未加载，无法收集资产。")
            return

        self._log("asset", f"开始收集资产，共 {len(self.md_files)} 个 MD 文件...")
        try:
            result = self.md_module.collect_assets_batch(self.md_files)
        except Exception as e:
            self._log("asset", f"收集资产失败: {e}")
            return

        self.episode_assets = result.get("episodes", {})
        global_assets = result.get("global", {})

        # 合并到 asset_mapping，保留已有路径
        for atype in ("角色", "场景", "道具", "音频", "视频"):
            if atype not in self.asset_mapping:
                self.asset_mapping[atype] = {}
            for name in global_assets.get(atype, set()):
                if name not in self.asset_mapping[atype]:
                    self.asset_mapping[atype][name] = ""

        # 更新 Combobox 选项
        episodes = ["全局（全部集）"] + sorted(self.episode_assets.keys())
        self.asset_view_combo['values'] = episodes
        self.asset_view_mode = "全局（全部集）"
        self.asset_view_var.set(self.asset_view_mode)

        # 刷新 Treeview
        self._refresh_asset_tree()

        n_char = len(self.asset_mapping.get("角色", {}))
        n_scene = len(self.asset_mapping.get("场景", {}))
        n_prop = len(self.asset_mapping.get("道具", {}))
        n_audio = len(self.asset_mapping.get("音频", {}))
        n_video = len(self.asset_mapping.get("视频", {}))
        self._log("asset", f"资产收集完成: 角色 {n_char}, 场景 {n_scene}, 道具 {n_prop}, 音频 {n_audio}, 视频 {n_video}")
        self._log("asset", "可在下方为每个资产填写 input 路径，完成后保存映射。")

    def _get_current_assets(self):
        """返回当前视图下的 (类型, 名称) 列表。"""
        result = []
        if self.asset_view_mode == "全局（全部集）":
            for atype in ("角色", "场景", "道具", "音频", "视频"):
                for name in sorted(self.asset_mapping.get(atype, {})):
                    result.append((atype, name))
        else:
            ep = self.episode_assets.get(self.asset_view_mode, {})
            for atype in ("角色", "场景", "道具", "音频", "视频"):
                for name in sorted(ep.get(atype, set())):
                    result.append((atype, name))
        return result

    def _refresh_asset_tree(self):
        """根据当前视图刷新 Treeview 内容。"""
        for item in self.asset_tree.get_children():
            self.asset_tree.delete(item)

        assets = self._get_current_assets()
        for idx, (atype, name) in enumerate(assets, 1):
            path = self.asset_mapping.get(atype, {}).get(name, "")
            self.asset_tree.insert("", tk.END, values=(idx, atype, name, path))

        self._asset_update_stats()

    def _asset_switch_view(self, event=None):
        """切换视图（全局 / 各集）。"""
        self.asset_view_mode = self.asset_view_var.get()
        self._refresh_asset_tree()
        self._log("asset", f"切换视图: {self.asset_view_mode}")

    def _asset_browse(self):
        """浏览选择文件，将路径填入选中行。"""
        selection = self.asset_tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先在资产列表中选中一行。")
            return

        item = selection[0]
        values = self.asset_tree.item(item, "values")
        idx, atype, name, old_path = values

        if atype == "音频":
            filetypes = [
                ("音频文件", "*.wav *.mp3 *.flac *.ogg *.aac *.m4a"),
                ("WAV", "*.wav"),
                ("MP3", "*.mp3"),
                ("所有文件", "*.*"),
            ]
            browse_title = f"选择 {name} 的参考音频"
        elif atype == "视频":
            filetypes = [
                ("视频/图片文件", "*.mp4 *.avi *.mov *.mkv *.webm *.png *.jpg *.jpeg"),
                ("视频", "*.mp4 *.avi *.mov *.mkv *.webm"),
                ("图片", "*.png *.jpg *.jpeg *.webp"),
                ("所有文件", "*.*"),
            ]
            browse_title = f"选择 {name} 的参考视频"
        else:
            filetypes = [
                ("图片文件", "*.png *.jpg *.jpeg *.webp"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("WebP", "*.webp"),
                ("所有文件", "*.*"),
            ]
            browse_title = f"选择 {name} 的参考图"
        path = filedialog.askopenfilename(
            title=browse_title, filetypes=filetypes,
        )
        if not path:
            return

        # 更新内部数据
        if atype not in self.asset_mapping:
            self.asset_mapping[atype] = {}
        self.asset_mapping[atype][name] = path

        # 更新 Treeview 行
        self.asset_tree.item(item, values=(idx, atype, name, path))
        self._asset_update_stats()
        self._log("asset", f"已设置 {atype}/{name} -> {path}")

    def _asset_edit_path(self):
        """手动编辑选中行的路径（弹出输入对话框）。"""
        selection = self.asset_tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先在资产列表中选中一行。")
            return

        item = selection[0]
        values = self.asset_tree.item(item, "values")
        idx, atype, name, old_path = values

        hint = "h3_audio/bgm.wav" if atype == "音频" else "h3_video/ref.mp4" if atype == "视频" else "h3_ref/角色/黑猫.png"
        new_path = simpledialog.askstring(
            "手动编辑路径",
            f"输入 {atype}/{name} 的 input 路径:\n"
            f"(可填 ComfyUI input 目录的相对路径，如 {hint})",
            initialvalue=old_path,
        )
        if new_path is None:
            return

        new_path = new_path.strip()
        if atype not in self.asset_mapping:
            self.asset_mapping[atype] = {}
        self.asset_mapping[atype][name] = new_path

        self.asset_tree.item(item, values=(idx, atype, name, new_path))
        self._asset_update_stats()
        self._log("asset", f"已编辑 {atype}/{name} -> {new_path}")

    def _asset_save_mapping(self):
        """将当前所有资产路径保存为 JSON 文件。"""
        if not self.asset_mapping:
            messagebox.showwarning("提示", "当前没有资产映射可保存。")
            return
        if self.md_module is None:
            messagebox.showerror("错误", "shot_md_to_excel.py 模块未加载，无法保存。")
            return

        path = filedialog.asksaveasfilename(
            title="保存资产映射",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialfile="asset_mapping.json",
        )
        if not path:
            return

        try:
            self.md_module.save_asset_mapping(self.asset_mapping, path)
            self.last_mapping_path = path
            self._log("asset", f"映射已保存: {path}")
            messagebox.showinfo("成功", f"映射已保存到:\n{path}")
        except Exception as e:
            self._log("asset", f"保存失败: {e}")
            messagebox.showerror("错误", f"保存失败: {e}")

    def _asset_load_mapping(self):
        """从 JSON 文件加载已有映射，填充到 Treeview。"""
        path = filedialog.askopenfilename(
            title="加载资产映射",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("错误", f"加载失败: {e}")
            return

        # 合并到 asset_mapping
        for atype in ("角色", "场景", "道具", "音频", "视频"):
            if atype not in self.asset_mapping:
                self.asset_mapping[atype] = {}
            loaded = data.get(atype, {})
            if isinstance(loaded, dict):
                for name, p in loaded.items():
                    self.asset_mapping[atype][name] = p

        self.last_mapping_path = path
        self._refresh_asset_tree()
        self._log("asset", f"映射已加载: {path}")

    def _asset_update_stats(self):
        """实时更新统计标签。"""
        n_char = len(self.asset_mapping.get("角色", {}))
        n_scene = len(self.asset_mapping.get("场景", {}))
        n_prop = len(self.asset_mapping.get("道具", {}))
        n_audio = len(self.asset_mapping.get("音频", {}))
        n_video = len(self.asset_mapping.get("视频", {}))
        total = n_char + n_scene + n_prop + n_audio + n_video

        filled = 0
        for atype in ("角色", "场景", "道具", "音频", "视频"):
            for p in self.asset_mapping.get(atype, {}).values():
                if p:
                    filled += 1

        self.asset_stats_label.config(
            text=f"角色: {n_char}  场景: {n_scene}  道具: {n_prop}  音频: {n_audio}  视频: {n_video}  已填: {filled}/{total}"
        )

    # ── Tab 3 方法 ──

    def _json_add_files(self):
        """添加 Excel 文件（支持多选）。"""
        files = filedialog.askopenfilenames(
            title="选择审阅表 Excel 文件",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
        )
        if not files:
            return
        existing = set(self.json_listbox.get(0, tk.END))
        for f in files:
            if f not in existing:
                self.json_listbox.insert(tk.END, f)

    def _json_remove_selected(self):
        """删除 Listbox 中选中的项。"""
        selected = list(self.json_listbox.curselection())
        for i in reversed(selected):
            self.json_listbox.delete(i)

    def _json_clear(self):
        """清空 Excel 文件列表。"""
        self.json_listbox.delete(0, tk.END)

    def _json_browse_output(self):
        """浏览选择输出目录。"""
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.json_output_entry.delete(0, tk.END)
            self.json_output_entry.insert(0, d)

    def _json_browse_mapping(self):
        """浏览选择资产映射 JSON 文件。"""
        path = filedialog.askopenfilename(
            title="选择资产映射 JSON 文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self._set_json_mapping_entry(path)

    def _json_use_tab2_mapping(self):
        """自动填入 Tab2 保存的映射路径。"""
        if self.last_mapping_path:
            self._set_json_mapping_entry(self.last_mapping_path)
            self._log("json", f"已填入 Tab2 映射: {self.last_mapping_path}")
        else:
            messagebox.showwarning(
                "提示",
                "Tab2 尚未保存过映射文件。\n请先在「资产管理」标签页保存映射。",
            )

    def _set_json_mapping_entry(self, path):
        """设置映射路径 Entry（只读控件需临时切换状态）。"""
        self.json_mapping_entry.config(state=tk.NORMAL)
        self.json_mapping_entry.delete(0, tk.END)
        self.json_mapping_entry.insert(0, path)
        self.json_mapping_entry.config(state="readonly")

    def _json_start(self):
        """启动 Excel → JSON 处理（后台线程）。"""
        if self.excel_module is None:
            messagebox.showerror("错误", "excel_to_multi_chain_json.py 模块未加载，无法执行。")
            return

        files = list(self.json_listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 Excel 文件。")
            return

        output_dir = self.json_output_entry.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return

        by_shot = (self.json_group_mode.get() == "by_shot")
        mapping_path = self.json_mapping_entry.get().strip()

        # 禁用按钮，防止重复点击
        self.json_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._json_worker,
            args=(files, output_dir, by_shot, mapping_path),
            daemon=True,
        )
        thread.start()

    def _json_worker(self, xlsx_files, output_dir, by_shot, mapping_path):
        """后台线程：执行 Excel → JSON 转换。"""
        log = lambda msg: self._log("json", msg)
        success, fail = 0, 0

        try:
            # 如果映射路径非空，加载资产映射
            asset_mapping = None
            if mapping_path:
                try:
                    asset_mapping = self.excel_module.load_asset_mapping(mapping_path)
                    log(f"已加载资产映射: {len(asset_mapping)} 条 from {mapping_path}")
                except Exception as e:
                    log(f"加载资产映射失败: {e}")

            log(f"=== 开始处理: {len(xlsx_files)} 个 Excel 文件 ===")
            log(f"输出目录: {output_dir}")
            log(f"分组模式: {'按镜头' if by_shot else '按角色'}")
            log("")

            if len(xlsx_files) == 1:
                # 单文件模式
                xlsx_path = xlsx_files[0]
                log(f"[1/1] {Path(xlsx_path).name}")
                result = self.excel_module.process_single(
                    xlsx_path, output_dir, by_shot=by_shot, log=log,
                    asset_mapping=asset_mapping,
                )
                success, fail = (1, 0) if result else (0, 1)
            else:
                # 批量模式
                success, fail = self.excel_module.process_batch(
                    xlsx_files, output_dir, by_shot=by_shot, log=log,
                    asset_mapping=asset_mapping,
                )

            log(f"\n=== 完成: {success} 成功, {fail} 失败 ===")

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())

        finally:
            self.log_queue.put(("done", "json", success, fail))

    # ── Tab 4: .pt 元数据读取器 ──

    def _build_tab_pt_reader(self, parent):
        """构建 .pt 元数据读取器标签页。"""
        # 顶部：选择文件/目录
        input_frame = ttk.LabelFrame(parent, text="选择 .pt 文件或目录")
        input_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        btn_container = ttk.Frame(input_frame)
        btn_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(btn_container, text="选择 .pt 文件",
                   command=self._pt_add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_container, text="选择目录",
                   command=self._pt_browse_dir).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_container, text="扫描并读取",
                   command=self._pt_scan).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_container, text="清空列表",
                   command=self._pt_clear).pack(side=tk.LEFT, padx=4)

        # 当前扫描路径显示
        path_container = ttk.Frame(input_frame)
        path_container.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(path_container, text="扫描路径:").pack(side=tk.LEFT)
        self.pt_path_var = tk.StringVar(value="")
        ttk.Entry(path_container, textvariable=self.pt_path_var,
                  state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # 结果 Treeview
        result_frame = ttk.LabelFrame(parent, text="元数据列表")
        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        tree_container = ttk.Frame(result_frame)
        tree_container.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 2))

        self.pt_tree = ttk.Treeview(
            tree_container,
            columns=("filename", "duration", "resolution", "frames", "size", "prompt", "ref_imgs", "error"),
            show="headings", height=12,
        )
        self.pt_tree.heading("filename", text="文件名")
        self.pt_tree.heading("duration", text="时长(秒)")
        self.pt_tree.heading("resolution", text="分辨率")
        self.pt_tree.heading("frames", text="帧数")
        self.pt_tree.heading("size", text="大小(MB)")
        self.pt_tree.heading("prompt", text="提示词")
        self.pt_tree.heading("ref_imgs", text="参考图数")
        self.pt_tree.heading("error", text="错误")
        self.pt_tree.column("filename", width=180, anchor=tk.W)
        self.pt_tree.column("duration", width=70, anchor=tk.CENTER)
        self.pt_tree.column("resolution", width=90, anchor=tk.CENTER)
        self.pt_tree.column("frames", width=60, anchor=tk.CENTER)
        self.pt_tree.column("size", width=70, anchor=tk.CENTER)
        self.pt_tree.column("prompt", width=200, anchor=tk.W)
        self.pt_tree.column("ref_imgs", width=70, anchor=tk.CENTER)
        self.pt_tree.column("error", width=150, anchor=tk.W)

        pt_scroll = ttk.Scrollbar(tree_container, orient=tk.VERTICAL,
                                  command=self.pt_tree.yview)
        self.pt_tree.configure(yscrollcommand=pt_scroll.set)
        self.pt_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        pt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 选中行时显示完整提示词
        self.pt_tree.bind("<<TreeviewSelect>>", self._pt_on_select)

        # 提示词预览
        preview_frame = ttk.LabelFrame(parent, text="提示词预览（选中行查看完整提示词）")
        preview_frame.pack(fill=tk.BOTH, expand=False, padx=10, pady=(5, 5))
        self.pt_prompt_preview = scrolledtext.ScrolledText(
            preview_frame, height=6, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.pt_prompt_preview.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=False, padx=10, pady=(5, 10))
        self.pt_log = scrolledtext.ScrolledText(
            log_frame, height=6, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.pt_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    def _pt_add_files(self):
        """选择单个或多个 .pt 文件。"""
        files = filedialog.askopenfilenames(
            title="选择 .pt 文件",
            filetypes=[("PyTorch .pt 文件", "*.pt"), ("所有文件", "*.*")],
        )
        if files:
            self.pt_path_var.set("; ".join(files))

    def _pt_browse_dir(self):
        """选择包含 .pt 文件的目录。"""
        d = filedialog.askdirectory(title="选择包含 .pt 文件的目录")
        if d:
            self.pt_path_var.set(d)

    def _pt_clear(self):
        """清空结果列表和预览。"""
        for item in self.pt_tree.get_children():
            self.pt_tree.delete(item)
        self.pt_prompt_preview.configure(state=tk.NORMAL)
        self.pt_prompt_preview.delete("1.0", tk.END)
        self.pt_prompt_preview.configure(state=tk.DISABLED)

    def _pt_find_comfy_python(self):
        """查找 ComfyUI 的 Python（带 torch）。"""
        candidates = [
            r"F:\02aidraw\ComfyUI-aki-v3\python\python.exe",
            r"F:\02aidraw\ComfyUI-aki-v3\python_embeded\python.exe",
            os.path.join(os.path.dirname(TOOLS_DIR), "..", "..", "python", "python.exe"),
            sys.executable,
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    def _pt_scan(self):
        """扫描并读取 .pt 文件元数据（后台线程）。"""
        path_str = self.pt_path_var.get().strip()
        if not path_str:
            messagebox.showwarning("提示", "请先选择 .pt 文件或目录。")
            return

        # 查找 ComfyUI Python
        comfy_python = self._pt_find_comfy_python()
        if not comfy_python:
            messagebox.showerror("错误", "未找到 ComfyUI 的 Python 环境（需要 torch）。\n"
                                 "请确认 ComfyUI 安装路径。")
            return

        # 查找 pt_meta_reader.py
        meta_reader = os.path.join(TOOLS_DIR, "pt_meta_reader.py")
        if not os.path.isfile(meta_reader):
            messagebox.showerror("错误", f"未找到 pt_meta_reader.py:\n{meta_reader}")
            return

        # 支持多路径（分号分隔）
        paths = [p.strip() for p in path_str.split(";") if p.strip()]
        if len(paths) == 1:
            target = paths[0]
        else:
            # 多文件时取第一个（pt_meta_reader 一次处理一个路径）
            target = paths[0]

        # 清空旧结果
        for item in self.pt_tree.get_children():
            self.pt_tree.delete(item)

        # 启动后台线程
        thread = threading.Thread(
            target=self._pt_scan_worker,
            args=(comfy_python, meta_reader, target),
            daemon=True,
        )
        thread.start()

    def _pt_scan_worker(self, comfy_python, meta_reader, target):
        """后台线程：调用 pt_meta_reader.py 读取元数据。"""
        log = lambda msg: self._log("pt", msg)

        try:
            log(f"=== 开始扫描: {target} ===")
            log(f"Python: {comfy_python}")
            log(f"脚本: {meta_reader}")

            cmd = [comfy_python, meta_reader, target, "--json"]
            log(f"执行: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if result.returncode != 0:
                log(f"[错误] 返回码: {result.returncode}")
                log(f"stderr: {result.stderr[:500]}")
                self.log_queue.put(("done", "pt", 0, 1))
                return

            # 解析 JSON 输出
            try:
                metadata_list = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                log(f"[错误] JSON 解析失败: {e}")
                log(f"stdout 前200字符: {result.stdout[:200]}")
                self.log_queue.put(("done", "pt", 0, 1))
                return

            if not metadata_list:
                log("未找到 .pt 文件。")
                self.log_queue.put(("done", "pt", 0, 0))
                return

            log(f"找到 {len(metadata_list)} 个 .pt 文件，开始解析...")

            # 将结果发送到主线程更新 UI
            self.log_queue.put(("pt_results", metadata_list))

        except Exception as e:
            log(f"[错误] 扫描过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())
            self.log_queue.put(("done", "pt", 0, 1))

    def _pt_populate_results(self, metadata_list):
        """在主线程中填充 Treeview 结果。"""
        success_count = 0
        error_count = 0

        for item in metadata_list:
            filename = item.get("filename", "?")
            duration = item.get("duration")
            width = item.get("width")
            height = item.get("height")
            frame_count = item.get("frame_count")
            size_mb = item.get("size_mb")
            has_prompt = item.get("has_prompt", False)
            prompt_preview = item.get("prompt_preview") or ""
            ref_image_count = item.get("ref_image_count", 0)
            error = item.get("error")

            # 格式化显示
            dur_str = f"{duration:.1f}" if duration is not None else "-"
            res_str = f"{int(width)}x{int(height)}" if width and height else "-"
            frame_str = str(frame_count) if frame_count is not None else "-"
            size_str = f"{size_mb:.1f}" if size_mb is not None else "-"
            prompt_str = prompt_preview[:80] + ("..." if len(prompt_preview) > 80 else "") if prompt_preview else ("无" if not has_prompt else "有")
            ref_str = str(ref_image_count) if ref_image_count else "0"
            err_str = error or ""

            if error:
                error_count += 1
            else:
                success_count += 1

            # 存储完整数据供选中预览使用
            self.pt_tree.insert("", tk.END, values=(
                filename, dur_str, res_str, frame_str, size_str,
                prompt_str, ref_str, err_str
            ), tags=(json.dumps(item),))

        self._log("pt", f"扫描完成: {success_count} 成功, {error_count} 错误")
        self.log_queue.put(("done", "pt", success_count, error_count))

    def _pt_on_select(self, event=None):
        """选中行时显示完整提示词。"""
        selection = self.pt_tree.selection()
        if not selection:
            return

        item = selection[0]
        tags = self.pt_tree.item(item, "tags")
        if not tags:
            return

        try:
            data = json.loads(tags[0])
        except (json.JSONDecodeError, IndexError):
            return

        prompt_text = data.get("prompt_preview") or ""
        if not prompt_text:
            prompt_text = data.get("error") or "(无提示词)"

        self.pt_prompt_preview.configure(state=tk.NORMAL)
        self.pt_prompt_preview.delete("1.0", tk.END)
        self.pt_prompt_preview.insert("1.0", prompt_text)
        self.pt_prompt_preview.configure(state=tk.DISABLED)

    # ── Tab 5: 资产准备表 → Krea2 JSON ──

    def _build_tab_asset_krea2(self, parent):
        """构建资产准备表 → Krea2 JSON 标签页。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="美术资产准备表 MD 文件")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.krea2_listbox = tk.Listbox(
            list_container, height=6, selectmode=tk.EXTENDED,
            font=("Consolas", 9),
        )
        krea2_scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                     command=self.krea2_listbox.yview)
        self.krea2_listbox.configure(yscrollcommand=krea2_scroll.set)
        self.krea2_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        krea2_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮行
        btn_frame = ttk.Frame(file_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(btn_frame, text="添加文件",
                   command=self._krea2_add_files).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="删除选中",
                   command=self._krea2_remove_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="清空",
                   command=self._krea2_clear).pack(side=tk.LEFT, padx=4)

        # 输出目录
        out_frame = ttk.LabelFrame(parent, text="输出目录（Krea2 JSON 工作流文件）")
        out_frame.pack(fill=tk.X, padx=10, pady=5)

        out_container = ttk.Frame(out_frame)
        out_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(out_container, text="输出目录:").pack(side=tk.LEFT)
        self.krea2_output_entry = ttk.Entry(out_container)
        self.krea2_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(out_container, text="浏览",
                   command=self._krea2_browse_output).pack(side=tk.LEFT)

        # 说明文字
        info_frame = ttk.LabelFrame(parent, text="说明")
        info_frame.pack(fill=tk.X, padx=10, pady=5)
        info_text = (
            "读取「美术资产准备表 MD」，自动生成 Krea2 ComfyUI 工作流 JSON：\n"
            "  • 角色资产 → identity_edit 锁脸多景别工作流（需参考图路径 + 角色 LoRA）\n"
            "  • 场景资产 → txt2img 批量文生图工作流（easy promptLine 批量提示词）\n"
            "  • 道具资产 → txt2img 单图设定图工作流\n"
            "生成的 JSON 可直接拖入 ComfyUI 加载执行。"
        )
        ttk.Label(info_frame, text=info_text, justify=tk.LEFT,
                  font=("Microsoft YaHei UI", 9)).pack(padx=8, pady=8)

        # 开始按钮
        self.krea2_run_btn = tk.Button(
            parent, text="开始生成 Krea2 JSON", height=2,
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#4a90d9", fg="white",
            activebackground="#5ba0e9", activeforeground="white",
            disabledforeground="#cccccc",
            command=self._krea2_start,
        )
        self.krea2_run_btn.pack(fill=tk.X, padx=20, pady=10)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.krea2_log = scrolledtext.ScrolledText(
            log_frame, height=12, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.krea2_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    def _krea2_add_files(self):
        """添加 MD 文件（支持多选）。"""
        files = filedialog.askopenfilenames(
            title="选择美术资产准备表 MD 文件",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
        )
        if not files:
            return
        existing = set(self.krea2_listbox.get(0, tk.END))
        for f in files:
            if f not in existing:
                self.krea2_listbox.insert(tk.END, f)

    def _krea2_remove_selected(self):
        """删除 Listbox 中选中的项。"""
        selected = list(self.krea2_listbox.curselection())
        for i in reversed(selected):
            self.krea2_listbox.delete(i)

    def _krea2_clear(self):
        """清空 MD 文件列表。"""
        self.krea2_listbox.delete(0, tk.END)

    def _krea2_browse_output(self):
        """浏览选择输出目录。"""
        d = filedialog.askdirectory(title="选择 Krea2 JSON 输出目录")
        if d:
            self.krea2_output_entry.delete(0, tk.END)
            self.krea2_output_entry.insert(0, d)

    def _krea2_start(self):
        """启动资产准备表 → Krea2 JSON 处理（后台线程）。"""
        if self.asset_krea2_module is None:
            messagebox.showerror("错误", "asset_md_to_krea2_json.py 模块未加载，无法执行。")
            return

        files = list(self.krea2_listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 MD 文件。")
            return

        output_dir = self.krea2_output_entry.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return

        # 禁用按钮，防止重复点击
        self.krea2_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._krea2_worker, args=(files, output_dir), daemon=True,
        )
        thread.start()

    def _krea2_worker(self, md_files, output_dir):
        """后台线程：执行资产准备表 MD → Krea2 JSON 转换。"""
        log = lambda msg: self._log("krea2", msg)
        total_chars, total_scenes, total_props = 0, 0, 0
        total_files = []

        try:
            log(f"=== 开始处理: {len(md_files)} 个 MD 文件 ===")
            log(f"输出目录: {output_dir}")
            log("")

            for i, md_path in enumerate(md_files, 1):
                log(f"[{i}/{len(md_files)}] {Path(md_path).name}")
                try:
                    result = self.asset_krea2_module.process_single(md_path, output_dir)
                    total_chars += result["characters"]
                    total_scenes += result["scenes"]
                    total_props += result["props"]
                    total_files.extend(result["files"])
                except Exception as e:
                    log(f"  [错误] {e}")
                    import traceback
                    log(traceback.format_exc())
                log("")

            log(f"=== 完成: 共生成 {len(total_files)} 个 JSON ===")
            log(f"  角色: {total_chars}, 场景: {total_scenes}, 道具: {total_props}")

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())

        finally:
            self.log_queue.put(("done", "krea2", len(total_files), 0))

    # ── 公共方法 ──

    def _log(self, tab, msg):
        """将日志消息放入队列（线程安全）。"""
        self.log_queue.put(("log", tab, str(msg)))

    def _get_log_widget(self, tab):
        """根据标签页名称获取对应的日志控件。"""
        mapping = {
            "md": getattr(self, "md_log", None),
            "asset": getattr(self, "asset_log", None),
            "json": getattr(self, "json_log", None),
            "pt": getattr(self, "pt_log", None),
            "krea2": getattr(self, "krea2_log", None),
        }
        return mapping.get(tab)

    def _poll_log_queue(self):
        """主线程轮询队列：写入日志 + 处理完成事件 + 资产收集。"""
        while not self.log_queue.empty():
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break

            msg_type = item[0]

            if msg_type == "log":
                _, tab, message = item
                widget = self._get_log_widget(tab)
                if widget:
                    try:
                        widget.configure(state=tk.NORMAL)
                        widget.insert(tk.END, message + "\n")
                        widget.see(tk.END)
                        widget.configure(state=tk.DISABLED)
                    except tk.TclError:
                        # 控件可能已销毁
                        pass

            elif msg_type == "done":
                _, tab, success, fail = item
                self._handle_done(tab, success, fail)

            elif msg_type == "assets_ready":
                self._populate_asset_tab()

            elif msg_type == "md_report_ready":
                self._md_show_report()

            elif msg_type == "pt_results":
                _, metadata_list = item
                self._pt_populate_results(metadata_list)

        self.root.after(100, self._poll_log_queue)

    def _handle_done(self, tab, success, fail):
        """处理任务完成事件，恢复按钮并弹窗提示。"""
        if tab == "md":
            self.md_run_btn.configure(state=tk.NORMAL)
            title = "MD → Excel 完成"
            # 切换到资产管理标签页
            self.notebook.select(self.tab_asset)
        elif tab == "json":
            self.json_run_btn.configure(state=tk.NORMAL)
            title = "Excel → JSON 完成"
        elif tab == "pt":
            title = ".pt 元数据读取完成"
        elif tab == "krea2":
            self.krea2_run_btn.configure(state=tk.NORMAL)
            title = "资产准备表 → Krea2 JSON 完成"
        else:
            return

        msg = f"处理完成！\n成功: {success} 个\n失败: {fail} 个"
        if fail > 0:
            messagebox.showwarning(title, msg)
        else:
            messagebox.showinfo(title, msg)


# ── 启动 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = H3ToolsApp(root)
    root.mainloop()
