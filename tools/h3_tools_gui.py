#!/usr/bin/env python3
"""H3 分镜工具箱 - 独立桌面 GUI 工具

三标签页：
  Tab 1: MD → Excel（分镜头需求MD转审阅表Excel）
  Tab 2: 资产管理（收集/映射/上传美术资产路径）
  Tab 3: Excel → JSON（审阅表Excel转多链生产JSON）

依赖同目录的 shot_md_to_excel.py 和 excel_to_multi_chain_json.py。
"""

import os
import sys
import json
import threading
import queue
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
    """H3 分镜工具箱主应用。"""

    def __init__(self, root):
        self.root = root
        self.root.title("H3 分镜工具箱")
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
        """构建主界面，包含三个标签页。"""
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

    # ── Tab 1: MD → Excel ──

    def _build_tab_md_to_excel(self, parent):
        """构建 MD → Excel 标签页。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="分镜头需求 MD 文件")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.md_listbox = tk.Listbox(
            list_container, height=6, selectmode=tk.EXTENDED,
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

        # 开始按钮（tkinter default 样式, 高度2）
        self.md_run_btn = tk.Button(
            parent, text="开始生成", height=2,
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#4a90d9", fg="white",
            activebackground="#5ba0e9", activeforeground="white",
            disabledforeground="#cccccc",
            command=self._md_start,
        )
        self.md_run_btn.pack(fill=tk.X, padx=20, pady=10)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.md_log = scrolledtext.ScrolledText(
            log_frame, height=12, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD,
        )
        self.md_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

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
        """添加 MD 文件（支持多选）。"""
        files = filedialog.askopenfilenames(
            title="选择分镜头需求 MD 文件",
            filetypes=[("Markdown 文件", "*.md"), ("所有文件", "*.*")],
        )
        if not files:
            return
        existing = set(self.md_listbox.get(0, tk.END))
        for f in files:
            if f not in existing:
                self.md_listbox.insert(tk.END, f)

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

        # 保存 MD 文件列表，供 Tab2 资产收集使用
        self.md_files = list(files)

        # 禁用按钮，防止重复点击
        self.md_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._md_worker, args=(files, output_dir), daemon=True,
        )
        thread.start()

    def _md_worker(self, md_files, output_dir):
        """后台线程：执行 MD → Excel 转换。"""
        log = lambda msg: self._log("md", msg)
        success, fail = 0, 0

        try:
            log(f"=== 开始处理: {len(md_files)} 个 MD 文件 ===")
            log(f"输出目录: {output_dir}")
            log("")

            if len(md_files) == 1:
                # 单文件模式：输出到输出目录下的同名 Excel
                md_path = md_files[0]
                stem = Path(md_path).stem
                out_name = stem.replace("分镜头需求", "提示词审阅表") + ".xlsx"
                out_path = os.path.join(output_dir, out_name)

                log(f"[1/1] {Path(md_path).name}")
                ok = self.md_module.process_single(md_path, out_path, log=log)
                success, fail = (1, 0) if ok else (0, 1)
            else:
                # 批量模式
                success, fail = self.md_module.process_batch(
                    md_files, output_dir, log=log
                )

            log(f"\n=== 完成: {success} 成功, {fail} 失败 ===")

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())

        finally:
            # 无论成功失败，都通知主线程收集资产并切换 Tab
            self.log_queue.put(("assets_ready",))
            self.log_queue.put(("done", "md", success, fail))

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
        for atype in ("角色", "场景", "道具"):
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
        self._log("asset", f"资产收集完成: 角色 {n_char}, 场景 {n_scene}, 道具 {n_prop}")
        self._log("asset", "可在下方为每个资产填写 input 路径，完成后保存映射。")

    def _get_current_assets(self):
        """返回当前视图下的 (类型, 名称) 列表。"""
        result = []
        if self.asset_view_mode == "全局（全部集）":
            for atype in ("角色", "场景", "道具"):
                for name in sorted(self.asset_mapping.get(atype, {})):
                    result.append((atype, name))
        else:
            ep = self.episode_assets.get(self.asset_view_mode, {})
            for atype in ("角色", "场景", "道具"):
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

        filetypes = [
            ("图片文件", "*.png *.jpg *.jpeg *.webp"),
            ("PNG", "*.png"),
            ("JPEG", "*.jpg *.jpeg"),
            ("WebP", "*.webp"),
            ("所有文件", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title=f"选择 {name} 的参考图", filetypes=filetypes,
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

        new_path = simpledialog.askstring(
            "手动编辑路径",
            f"输入 {atype}/{name} 的 input 路径:\n"
            f"(可填 ComfyUI input 目录的相对路径，如 h3_ref/角色/黑猫.png)",
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
        for atype in ("角色", "场景", "道具"):
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
        total = n_char + n_scene + n_prop

        filled = 0
        for atype in ("角色", "场景", "道具"):
            for p in self.asset_mapping.get(atype, {}).values():
                if p:
                    filled += 1

        self.asset_stats_label.config(
            text=f"角色: {n_char}  场景: {n_scene}  道具: {n_prop}  已填: {filled}/{total}"
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
