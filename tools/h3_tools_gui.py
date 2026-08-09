#!/usr/bin/env python3
"""H3 分镜工具箱 - 独立桌面 GUI 工具

提供两个功能标签页：
  1. MD → Excel: 将分镜头需求 MD 文件转换为提示词审阅表 Excel
  2. Excel → JSON: 将审阅表 Excel 转换为多链生产 JSON

依赖同目录的 shot_md_to_excel.py 和 excel_to_multi_chain_json.py。
"""

import os
import sys
import threading
import queue
import importlib.util
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ── 动态导入同目录的脚本 ─────────────────────────────────────────────

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _import_module(filename, modname):
    """从文件路径动态导入 Python 模块。

    优先从 TOOLS_DIR 查找，找不到则尝试上级 tools 子目录。
    """
    filepath = os.path.join(TOOLS_DIR, filename)
    if not os.path.exists(filepath):
        # 尝试从 tools 子目录
        filepath = os.path.join(os.path.dirname(TOOLS_DIR), "tools", filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"找不到模块文件: {filename} (搜索目录: {TOOLS_DIR})")

    spec = importlib.util.spec_from_file_location(modname, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── GUI 应用类 ───────────────────────────────────────────────────────

class H3ToolsApp:
    """H3 分镜工具箱主应用。"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("H3 分镜工具箱")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        # 尝试导入两个处理模块
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

        # 消息队列（线程安全日志传递）
        # 队列元素: ("log", text_widget, message) 或 ("done", task_type, success, fail)
        self.log_queue = queue.Queue()

        # 应用 ttk 样式
        self._setup_style()

        # 构建 UI
        self._build_ui()

        # 窗口居中
        self._center_window()

        # 启动队列轮询
        self.root.after(100, self._poll_log_queue)

        # 如果有导入错误，延迟提示
        if self._import_error:
            self.root.after(500, lambda: messagebox.showwarning(
                "模块加载警告", self._import_error
            ))

    # ── 样式设置 ──

    def _setup_style(self):
        """配置 ttk 样式美化界面。"""
        style = ttk.Style()

        # 尝试使用更现代的主题
        available_themes = style.theme_names()
        for theme in ("clam", "vista", "xpnative"):
            if theme in available_themes:
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

        # 大按钮样式
        style.configure("Big.TButton",
                        font=("Microsoft YaHei UI", 12, "bold"),
                        padding=(20, 10))
        style.map("Big.TButton",
                  background=[("active", "#4a90d9"), ("disabled", "#cccccc")],
                  foreground=[("active", "#ffffff"), ("disabled", "#888888")])

        # 普通按钮
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(8, 4))

        # 标签
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel",
                        font=("Microsoft YaHei UI", 11, "bold"),
                        foreground="#333333")

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
        """构建主界面，包含两个标签页。"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Tab 1: MD → Excel
        self.tab_md = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_md, text="  MD → Excel  ")
        self._build_md_tab(self.tab_md)

        # Tab 2: Excel → JSON
        self.tab_excel = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_excel, text="  Excel → JSON  ")
        self._build_excel_tab(self.tab_excel)

    # ── Tab 1: MD → Excel ──

    def _build_md_tab(self, parent):
        """构建 MD → Excel 标签页。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="分镜头需求 MD 文件")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        # Listbox + 滚动条
        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.md_listbox = tk.Listbox(
            list_container,
            height=6,
            selectmode=tk.EXTENDED,
            font=("Consolas", 9),
        )
        list_scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                    command=self.md_listbox.yview)
        self.md_listbox.configure(yscrollcommand=list_scroll.set)
        self.md_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮行
        btn_frame = ttk.Frame(file_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(btn_frame, text="添加文件", command=self._md_add_files).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="删除选中", command=self._md_remove_selected).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="清空", command=self._md_clear).pack(
            side=tk.LEFT, padx=4)

        # 输出目录
        out_frame = ttk.LabelFrame(parent, text="输出目录")
        out_frame.pack(fill=tk.X, padx=10, pady=5)

        out_container = ttk.Frame(out_frame)
        out_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(out_container, text="输出目录:").pack(side=tk.LEFT)
        self.md_output_entry = ttk.Entry(out_container)
        self.md_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(out_container, text="浏览", command=self._md_browse_output).pack(
            side=tk.LEFT)

        # 开始按钮
        self.md_run_btn = ttk.Button(
            parent, text="开始生成", style="Big.TButton",
            command=self._md_run,
        )
        self.md_run_btn.pack(fill=tk.X, padx=20, pady=10)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.md_log = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            font=("Consolas", 9),
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.md_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Tab 2: Excel → JSON ──

    def _build_excel_tab(self, parent):
        """构建 Excel → JSON 标签页。"""
        # 文件列表区域
        file_frame = ttk.LabelFrame(parent, text="审阅表 Excel 文件")
        file_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        list_container = ttk.Frame(file_frame)
        list_container.pack(fill=tk.X, padx=8, pady=4)

        self.excel_listbox = tk.Listbox(
            list_container,
            height=6,
            selectmode=tk.EXTENDED,
            font=("Consolas", 9),
        )
        list_scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL,
                                    command=self.excel_listbox.yview)
        self.excel_listbox.configure(yscrollcommand=list_scroll.set)
        self.excel_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮行
        btn_frame = ttk.Frame(file_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=(2, 8))
        ttk.Button(btn_frame, text="添加文件", command=self._excel_add_files).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="删除选中", command=self._excel_remove_selected).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="清空", command=self._excel_clear).pack(
            side=tk.LEFT, padx=4)

        # 输出目录
        out_frame = ttk.LabelFrame(parent, text="输出目录")
        out_frame.pack(fill=tk.X, padx=10, pady=5)

        out_container = ttk.Frame(out_frame)
        out_container.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(out_container, text="输出目录:").pack(side=tk.LEFT)
        self.excel_output_entry = ttk.Entry(out_container)
        self.excel_output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(out_container, text="浏览", command=self._excel_browse_output).pack(
            side=tk.LEFT)

        # 分组模式
        mode_frame = ttk.LabelFrame(parent, text="分组模式")
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        mode_container = ttk.Frame(mode_frame)
        mode_container.pack(fill=tk.X, padx=8, pady=8)
        self.excel_group_mode = tk.StringVar(value="by_char")
        ttk.Radiobutton(
            mode_container, text="按角色分组(推荐)",
            variable=self.excel_group_mode, value="by_char",
        ).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Radiobutton(
            mode_container, text="按镜头分组(每镜一个JSON)",
            variable=self.excel_group_mode, value="by_shot",
        ).pack(side=tk.LEFT)

        # 开始按钮
        self.excel_run_btn = ttk.Button(
            parent, text="开始生成", style="Big.TButton",
            command=self._excel_run,
        )
        self.excel_run_btn.pack(fill=tk.X, padx=20, pady=10)

        # 日志区域
        log_frame = ttk.LabelFrame(parent, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        self.excel_log = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            font=("Consolas", 9),
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.excel_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── MD Tab: 文件操作 ──

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

    # ── Excel Tab: 文件操作 ──

    def _excel_add_files(self):
        """添加 Excel 文件（支持多选）。"""
        files = filedialog.askopenfilenames(
            title="选择审阅表 Excel 文件",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
        )
        if not files:
            return
        existing = set(self.excel_listbox.get(0, tk.END))
        for f in files:
            if f not in existing:
                self.excel_listbox.insert(tk.END, f)

    def _excel_remove_selected(self):
        """删除 Listbox 中选中的项。"""
        selected = list(self.excel_listbox.curselection())
        for i in reversed(selected):
            self.excel_listbox.delete(i)

    def _excel_clear(self):
        """清空 Excel 文件列表。"""
        self.excel_listbox.delete(0, tk.END)

    def _excel_browse_output(self):
        """浏览选择输出目录。"""
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.excel_output_entry.delete(0, tk.END)
            self.excel_output_entry.insert(0, d)

    # ── 日志写入（线程安全）──

    def _make_logger(self, text_widget):
        """创建一个日志函数，将消息通过 queue 传递给主线程。"""
        def log(message):
            self.log_queue.put(("log", text_widget, str(message)))
        return log

    def _poll_log_queue(self):
        """主线程轮询队列：写入日志 + 处理完成事件。"""
        while not self.log_queue.empty():
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break

            msg_type = item[0]

            if msg_type == "log":
                _, text_widget, message = item
                try:
                    text_widget.configure(state=tk.NORMAL)
                    text_widget.insert(tk.END, message + "\n")
                    text_widget.see(tk.END)
                    text_widget.configure(state=tk.DISABLED)
                except tk.TclError:
                    # 控件可能已销毁
                    pass

            elif msg_type == "done":
                _, task_type, success, fail = item
                self._handle_done(task_type, success, fail)

        self.root.after(100, self._poll_log_queue)

    # ── MD Tab: 执行处理 ──

    def _md_run(self):
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

        # 禁用按钮，防止重复点击
        self.md_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._md_worker,
            args=(files, output_dir),
            daemon=True,
        )
        thread.start()

    def _md_worker(self, md_files, output_dir):
        """后台线程：执行 MD → Excel 转换。"""
        log = self._make_logger(self.md_log)

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

            # 通过队列通知主线程完成
            self.log_queue.put(("done", "md", success, fail))

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())
            self.log_queue.put(("done", "md", 0, 1))

    # ── Excel Tab: 执行处理 ──

    def _excel_run(self):
        """启动 Excel → JSON 处理（后台线程）。"""
        if self.excel_module is None:
            messagebox.showerror("错误", "excel_to_multi_chain_json.py 模块未加载，无法执行。")
            return

        files = list(self.excel_listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 Excel 文件。")
            return

        output_dir = self.excel_output_entry.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择输出目录。")
            return

        by_shot = (self.excel_group_mode.get() == "by_shot")

        # 禁用按钮，防止重复点击
        self.excel_run_btn.configure(state=tk.DISABLED)

        # 启动后台线程
        thread = threading.Thread(
            target=self._excel_worker,
            args=(files, output_dir, by_shot),
            daemon=True,
        )
        thread.start()

    def _excel_worker(self, xlsx_files, output_dir, by_shot):
        """后台线程：执行 Excel → JSON 转换。"""
        log = self._make_logger(self.excel_log)

        try:
            log(f"=== 开始处理: {len(xlsx_files)} 个 Excel 文件 ===")
            log(f"输出目录: {output_dir}")
            log(f"分组模式: {'按镜头' if by_shot else '按角色'}")
            log("")

            if len(xlsx_files) == 1:
                # 单文件模式
                xlsx_path = xlsx_files[0]
                log(f"[1/1] {Path(xlsx_path).name}")
                result = self.excel_module.process_single(
                    xlsx_path, output_dir, by_shot=by_shot, log=log
                )
                if result:
                    success, fail = 1, 0
                else:
                    success, fail = 0, 1
            else:
                # 批量模式
                success, fail = self.excel_module.process_batch(
                    xlsx_files, output_dir, by_shot=by_shot, log=log
                )

            log(f"\n=== 完成: {success} 成功, {fail} 失败 ===")

            # 通过队列通知主线程完成
            self.log_queue.put(("done", "excel", success, fail))

        except Exception as e:
            log(f"\n[错误] 处理过程中发生异常: {e}")
            import traceback
            log(traceback.format_exc())
            self.log_queue.put(("done", "excel", 0, 1))

    # ── 任务完成回调 ──

    def _handle_done(self, task_type, success, fail):
        """处理任务完成事件，恢复按钮并弹窗提示。"""
        if task_type == "md":
            self.md_run_btn.configure(state=tk.NORMAL)
            title = "MD → Excel 完成"
        else:
            self.excel_run_btn.configure(state=tk.NORMAL)
            title = "Excel → JSON 完成"

        msg = f"处理完成！\n成功: {success} 个\n失败: {fail} 个"
        if fail > 0:
            messagebox.showwarning(title, msg)
        else:
            messagebox.showinfo(title, msg)

    # ── 主循环 ──

    def mainloop(self):
        """启动应用主循环。"""
        self.root.mainloop()


# ── 启动 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = H3ToolsApp()
    app.mainloop()
