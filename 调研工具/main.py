#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / main.py
核心修复：所有工作线程→GUI的通信，统一通过 queue.Queue，
主线程每100ms轮询执行。彻底绕过 after() 在Windows工作线程里的不可靠问题。
版本：v1.0（桌面版）
"""

import os, sys, json, threading, asyncio, subprocess, logging, queue
from datetime import datetime
from typing import List, Dict

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    sv_ttk = None
    HAS_SV_TTK = False

from config import load_config, save_config, apply_config_to_env, logger

from engine import FusionResearchBot
from deliverables import DeliverableCompiler
from task_state import StageFailed, list_unfinished_tasks


# ============================================================
# 主 GUI（基于 Queue 的跨线程通信）
# ============================================================

class ResearchGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("小醉桌面研究 v1.0")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(1450, max(1000, screen_w - 40))
        # 预留任务栏 + 标题栏/边框高度（约 140px），避免窗口底部落到任务栏之下
        win_h = min(1000, max(620, screen_h - 140))
        # 顶部贴 0 定位，防止默认层叠偏移把底部按钮顶到任务栏下面
        pos_x = max(0, (screen_w - win_w) // 2)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+0")
        self.root.minsize(1000, 600)
        self._setup_theme()

        # ---------- 线程同步 ----------
        self.start_event = threading.Event()
        self.clarify_event = threading.Event()
        self.plan_event = threading.Event()
        self.outline_event = threading.Event()

        # ---------- 数据交换 ----------
        self.task_text = ""
        self.clarify_answer = ""
        self.plan_result = None
        self.outline_action = None
        self.outline_text = ""
        self.waiting_state = None
        self.final_report_path = ""
        self.worker_thread = None
        self.resume_task_id = ""
        self.current_record = None
        self.last_failed_task = None

        self._chapters_md: List[str] = []
        self._action_plan: str = ""
        self._all_materials: List[Dict] = []

        # ---------- 跨线程队列（根治方案） ----------
        self.ui_queue = queue.Queue()
        self._scroll_canvases = {}
        self._poll_ui_queue()

        # ---------- 配置 & UI ----------
        # 统一使用本地 research_config.json
        self.config = load_config()
        apply_config_to_env(self.config)
        self.setup_ui()
        self.setup_logger()

        if not self.config.get("llm_api_key"):
            self.notebook.select(self.config_tab)
            self._safe_ui(lambda: self.status_label.config(text="请先填写 API Key 并保存"))

    def _setup_theme(self):
        """使用 sv-ttk 的 Windows 11 Fluent 浅色主题，并补充中文字体。"""
        self.colors = {
            "bg": "#FAFAFA",
            "card": "#FFFFFF",
            "primary": "#005FB8",
            "primary_dark": "#004C94",
            "text": "#1C1C1C",
            "muted": "#6B6B6B",
            "border": "#E5E5E5",
        }
        if HAS_SV_TTK:
            sv_ttk.set_theme("light", self.root)
        else:
            fallback = ttk.Style(self.root)
            try:
                fallback.theme_use("clam")
            except tk.TclError:
                pass
            fallback.configure("Card.TFrame", background=self.colors["card"])

        style = ttk.Style(self.root)
        self.style = style
        self.root.configure(bg=self.colors["bg"])

        # sv-ttk 使用命名字体，统一替换为微软雅黑以改善中文显示
        for name in ("SunValleyCaptionFont", "SunValleyBodyFont", "SunValleyBodyStrongFont",
                     "SunValleyBodyLargeFont", "SunValleySubtitleFont", "SunValleyTitleFont"):
            try:
                tkfont.nametofont(name).configure(family="微软雅黑")
            except tk.TclError:
                pass

        style.configure("Title.TLabel", background=self.colors["card"],
                        foreground=self.colors["primary"], font=("微软雅黑", 20, "bold"))
        style.configure("Subtitle.TLabel", background=self.colors["card"],
                        foreground=self.colors["muted"], font=("微软雅黑", 9))
        style.configure("Card.TLabel", background=self.colors["card"],
                        foreground=self.colors["text"], font=("微软雅黑", 10))
        style.configure("Section.TLabel", background=self.colors["bg"],
                        foreground=self.colors["text"], font=("微软雅黑", 10, "bold"))
        style.configure("Muted.TLabel", background=self.colors["bg"],
                        foreground=self.colors["muted"], font=("微软雅黑", 9))
        style.configure("Hint.TLabel", background=self.colors["bg"],
                        foreground=self.colors["muted"], font=("微软雅黑", 8))
        style.configure("Accent.TButton", font=("微软雅黑", 10, "bold"))
        style.configure("TButton", font=("微软雅黑", 10))
        style.configure("TNotebook.Tab", font=("微软雅黑", 10))

    # ---------- 跨线程队列轮询（主线程每100ms执行） ----------
    def _poll_ui_queue(self):
        """主线程专用：从队列取出UI更新任务并执行"""
        try:
            while True:
                task = self.ui_queue.get_nowait()
                if callable(task):
                    try:
                        task()
                    except Exception as e:
                        print(f"[UI任务错误] {e}")
                elif isinstance(task, tuple) and len(task) == 3:
                    func, args, kwargs = task
                    try:
                        func(*args, **kwargs)
                    except Exception as e:
                        print(f"[UI任务错误] {e}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _safe_ui(self, func):
        """工作线程调用：把UI更新函数放进队列，由主线程执行"""
        self.ui_queue.put(func)

    def _scroll_frame(self, parent):
        """创建主题化可滚动容器，返回 (canvas, 内容Frame)。"""
        canvas = tk.Canvas(parent, bg=self.colors["bg"], highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        frame = ttk.Frame(canvas)
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas._inner_window = window

        def _sync_width(event=None):
            width = canvas.winfo_width()
            if width > 1:
                canvas.itemconfigure(window, width=width)

        # 页面第一次被选中时才映射 Canvas，<Configure> 不一定会再触发，需要 Map 兜底
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind("<Map>", lambda e: canvas.after(60, _sync_width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._scroll_canvases[str(frame)] = canvas
        return canvas, frame

    def _make_text(self, parent, height=5, font=("微软雅黑", 10), padx=8, pady=8):
        """创建带 ttk 滚动条的文本区，返回 (容器, Text)。"""
        frame = ttk.Frame(parent)
        text = tk.Text(frame, height=height, wrap=tk.WORD, font=font, bg="#FFFFFF",
                       relief="flat", padx=padx, pady=pady, borderwidth=0,
                       highlightthickness=1, highlightbackground=self.colors["border"],
                       highlightcolor=self.colors["primary"])
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, text

    def _on_mousewheel(self, event):
        """滚轮滚动鼠标所在的滚动容器。"""
        if isinstance(event.widget, (tk.Text, tk.Listbox)):
            return
        widget = event.widget
        while widget is not None:
            canvas = self._scroll_canvases.get(str(widget))
            if canvas is not None:
                canvas.yview_scroll(int(-event.delta / 120), "units")
                return
            widget = widget.master

    # ---------- UI 搭建 ----------
    def setup_ui(self):
        header = ttk.Frame(self.root, style="Card.TFrame")
        header.pack(fill=tk.X, padx=12, pady=(12, 4))
        ttk.Label(header, text="小醉桌面研究", style="Title.TLabel").pack(anchor=tk.W, padx=16, pady=(12, 0))
        ttk.Label(header, text="输入需求 · 自动搜索 · 生成带来源的报告", style="Subtitle.TLabel").pack(anchor=tk.W, padx=16, pady=(0, 12))

        content = ttk.Frame(self.root)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left_outer = ttk.Frame(content, width=410)
        left_outer.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 4), pady=4)
        left_outer.pack_propagate(False)

        # 大纲操作按钮固定在左栏底部，不随滚动区域移出可见范围
        self.action_bar = ttk.Frame(left_outer)
        self.action_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=2, pady=(0, 4))

        left_canvas = tk.Canvas(left_outer, width=390, highlightthickness=0, bg=self.colors["bg"])
        left_scrollbar = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=left_canvas.yview)
        left = ttk.Frame(left_canvas, padding=12)
        left.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.create_window((0, 0), window=left, anchor="nw", width=380)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._scroll_canvases[str(left)] = left_canvas

        right = ttk.Frame(content)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        ttk.Label(left, text="调研需求", style="Section.TLabel").pack(anchor=tk.W, pady=(0, 6))
        task_frame, self.task_input = self._make_text(left, height=8, font=("微软雅黑", 10))
        task_frame.pack(fill=tk.X, pady=(0, 8))
        self.task_placeholder = "在此输入你的调研需求..."
        self.task_input.insert("1.0", self.task_placeholder)
        self.task_input.bind("<FocusIn>", self._clear_task_placeholder)
        self.task_input.bind("<FocusOut>", self._restore_task_placeholder)

        pages_frame = ttk.Frame(left)
        pages_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(pages_frame, text="报告页数期望：", font=("微软雅黑", 9)).pack(side=tk.LEFT)
        self.pages_var = tk.StringVar(value=str(self.config.get("max_pages", 15)))
        ttk.Spinbox(pages_frame, from_=5, to_=100, width=5, textvariable=self.pages_var, font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=5)
        ttk.Label(pages_frame, text="页", font=("微软雅黑", 9)).pack(side=tk.LEFT)

        # 报告模式选择
        mode_frame = ttk.Frame(left)
        mode_frame.pack(fill=tk.X, pady=(5,0))
        ttk.Label(mode_frame, text="报告模式：", font=("微软雅黑", 9)).pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="quick")
        ttk.Radiobutton(mode_frame, text="常规报告", variable=self.mode_var, value="quick",
                        command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="超深度", variable=self.mode_var, value="deep",
                        command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="行业搜索", variable=self.mode_var, value="industry",
                        command=self._on_mode_change).pack(side=tk.LEFT, padx=5)

        self.start_btn = ttk.Button(left, text="开始生成", command=self.on_start, style="Accent.TButton")
        self.start_btn.pack(fill=tk.X, pady=8)

        self.confirm_fw_btn = ttk.Button(left, text="确认框架并执行", command=self.on_confirm_framework)

        # 任务恢复：不做批量队列，失败任务单独重试
        self.retry_btn = ttk.Button(left, text="重试上次任务", command=self.on_retry_last_task)
        self.retry_btn.pack(fill=tk.X, pady=(0, 8))
        self.retry_btn.config(state=tk.DISABLED)

        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Label(left, text="控制面修改 / 反馈", style="Section.TLabel").pack(anchor=tk.W)
        feedback_frame, self.feedback_entry = self._make_text(left, height=4)
        feedback_frame.pack(fill=tk.X, pady=5)
        ttk.Label(left, text="例：改页数：第2章页数改5 ｜ 补搜：第2章补搜 竞争格局，市场份额",
                  style="Hint.TLabel", wraplength=370).pack(anchor=tk.W, pady=(0, 4))

        ttk.Separator(self.action_bar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 5))
        btn_frame = ttk.Frame(self.action_bar)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="确认通过", command=self.on_outline_confirm).pack(side=tk.LEFT, expand=True, padx=1)
        ttk.Button(btn_frame, text="应用修改", command=self.on_outline_edit).pack(side=tk.LEFT, expand=True, padx=1)
        ttk.Button(btn_frame, text="回炉重造", command=self.on_outline_remelt).pack(side=tk.LEFT, expand=True, padx=1)
        ttk.Button(btn_frame, text="补搜本章", command=self.on_outline_supplement).pack(side=tk.LEFT, expand=True, padx=1)
        self.root.bind_all("<Control-Return>", self._on_confirm_shortcut)

        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Label(left, text="当前状态", style="Section.TLabel").pack(anchor=tk.W)
        self.status_label = ttk.Label(left, text="等待开始", foreground=self.colors["primary"],
                                      font=("微软雅黑", 9, "bold"), wraplength=360)
        self.status_label.pack(anchor=tk.W, pady=2, fill=tk.X)
        self.progress = ttk.Progressbar(left, mode='indeterminate')

        self.progress.pack(fill=tk.X, pady=5)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.plan_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.plan_tab, text="调研计划")
        plan_frame, self.plan_text_widget = self._make_text(self.plan_tab, height=10,
                                                            font=("微软雅黑", 10), padx=10, pady=10)
        plan_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.plan_text_widget.tag_configure("hint", foreground="#9CA3AF")
        self.plan_text_widget.insert("1.0", "调研计划将在这里展示。\n\n在左侧输入调研需求后，点击【开始生成】。", "hint")

        self.outline_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.outline_tab, text="报告控制面")
        outline_frame, self.outline_text_widget = self._make_text(self.outline_tab, height=10,
                                                                  font=("微软雅黑", 10), padx=10, pady=10)
        outline_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.outline_text_widget.tag_configure("hint", foreground="#9CA3AF")
        self.outline_text_widget.insert("1.0", "报告骨架、控制面与补搜结果将在这里展示。", "hint")

        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.log_tab, text="执行日志")
        log_frame, self.log_text = self._make_text(self.log_tab, height=10,
                                                   font=("Consolas", 9), padx=10, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_text.config(state=tk.DISABLED)

        self.config_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.config_tab, text="配置")
        self._build_config_ui(self.config_tab)

        self.report_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.report_tab, text="最终报告")
        report_card = ttk.Frame(self.report_tab, style="Card.TFrame", padding=24)
        report_card.pack(expand=True, padx=24, pady=24)
        self.report_info = ttk.Label(report_card, text="报告尚未生成", style="Card.TLabel",
                                     font=("微软雅黑", 11), wraplength=700, justify=tk.CENTER)
        self.report_info.pack(pady=(0, 10), padx=32)
        ttk.Label(report_card, text="报告生成后可打开目录、复制路径，或把满意点沉淀为下次策略。",
                  style="Subtitle.TLabel", justify=tk.CENTER).pack(pady=(0, 16), padx=32)
        ttk.Button(report_card, text="打开报告所在文件夹", command=self.open_report_dir,
                   style="Accent.TButton", width=24).pack(pady=5)
        ttk.Button(report_card, text="复制报告路径", command=self.copy_report_path, width=24).pack(pady=5)
        ttk.Button(report_card, text="反馈并沉淀策略", command=self.on_collect_feedback, width=24).pack(pady=5)

        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self._on_mode_change(notify=False)
        self._load_last_failed_task()

    def _build_config_ui(self, parent):
        """四标签页配置界面"""
        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)

        # 标签页1: 功能配置
        tab_features = ttk.Frame(nb)
        nb.add(tab_features, text="功能配置")
        self._build_features_config_ui(tab_features)

        # 标签页2: 本地部署
        tab_local = ttk.Frame(nb)
        nb.add(tab_local, text="本地部署")
        self._build_local_deploy_ui(tab_local)

        # 标签页3: 常规搜索策略
        tab_regular = ttk.Frame(nb)
        nb.add(tab_regular, text="常规搜索策略")
        self._build_regular_report_ui(tab_regular)

        # 标签页4: 深度搜索策略
        tab_deep = ttk.Frame(nb)
        nb.add(tab_deep, text="深度搜索策略")
        self._build_deep_report_ui(tab_deep)

        # 切换配置页时强制同步滚动容器宽度，避免首次进入显示空白
        self.notebook.bind("<<NotebookTabChanged>>", self._schedule_scroll_sync, add="+")
        nb.bind("<<NotebookTabChanged>>", self._schedule_scroll_sync, add="+")

    def _schedule_scroll_sync(self, event=None):
        self.root.after(60, self._sync_scroll_canvases)
        self.root.after(200, self._sync_scroll_canvases)

    def _sync_scroll_canvases(self):
        for canvas in list(self._scroll_canvases.values()):
            try:
                if not canvas.winfo_ismapped():
                    continue
                width = canvas.winfo_width()
                window = getattr(canvas, "_inner_window", None)
                if width > 1 and window:
                    canvas.itemconfigure(window, width=width)
                    canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

    def _build_features_config_ui(self, parent):
        """功能配置：API、搜索接口、输出目录与全局功能开关。"""
        _, sf = self._scroll_frame(parent)

        def section(text):
            ttk.Label(sf, text=text, style="Section.TLabel").pack(anchor=tk.W, pady=(12, 4), padx=10)

        def add_field(label, key, width=50, show=""):
            frame = ttk.Frame(sf)
            frame.pack(fill=tk.X, pady=3, padx=10)
            ttk.Label(frame, text=label, width=20, anchor=tk.E).pack(side=tk.LEFT)
            var = tk.StringVar(value=self.config.get(key, ""))
            self.config_vars[key] = var
            ttk.Entry(frame, textvariable=var, width=width, show=show).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.config_vars = {}

        section("大模型配置")
        add_field("API Key", "llm_api_key", show="*")
        add_field("Base URL", "llm_base_url")
        add_field("计划模型", "plan_model", width=30)
        add_field("写作模型", "write_model", width=30)

        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8, padx=10)

        section("DeepSeek 搜索配置")
        add_field("搜索 Key（留空复用）", "search_api_key", width=40, show="*")
        add_field("搜索接口地址", "search_api_endpoint")

        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8, padx=10)

        section("输出与功能")
        add_field("输出目录", "output_dir")
        self.deep_scrape_var = tk.BooleanVar(value=self.config.get("enable_deep_scrape", True))
        ttk.Checkbutton(sf, text="高价值网页全文抓取（DeepSeek Fetch，成本更高）",
                        variable=self.deep_scrape_var).pack(anchor=tk.W, padx=20, pady=6)
        ttk.Label(sf, text="开启后素材分层存储自动启用；关闭可降低抓取成本。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20)

        self.storm_var = tk.BooleanVar(value=self.config.get("enable_storm_debate", False))
        ttk.Checkbutton(sf, text="STORM 多角度辩论（增加 6–8 条查询，耗时更长）",
                        variable=self.storm_var).pack(anchor=tk.W, padx=20, pady=6)
        ttk.Label(sf, text="默认关闭；打开后从支持者/质疑者/旁观者三个视角补充素材，查询计入搜索预算。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20)

        self.ai_fallback_var = tk.BooleanVar(
            value=self.config.get("enable_ai_knowledge_fallback", True))
        ttk.Checkbutton(sf, text="素材不足时用 AI 训练知识补章（标题标注【AI训练知识】）",
                        variable=self.ai_fallback_var).pack(anchor=tk.W, padx=20, pady=6)
        ttk.Label(sf, text="默认开启；关闭后日志会提示，可在【控制面修改】填关键词后点【补搜本章】重试。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20)

        ttk.Button(sf, text="保存配置", command=self.on_save_config,
                   style="Accent.TButton").pack(anchor=tk.W, pady=15, padx=10)
        self.config_status = ttk.Label(sf, text="", foreground="#16A34A", font=("微软雅黑", 9))
        self.config_status.pack(anchor=tk.W, padx=10)


    def _build_local_deploy_ui(self, parent):
        """本地部署：本地语义模型与内置能力说明。"""
        _, sf = self._scroll_frame(parent)

        ttk.Label(sf, text="本地语义模型", style="Section.TLabel").pack(anchor=tk.W, pady=(12, 4), padx=10)
        ttk.Label(sf, text="用于素材聚类与语义检索；未启动 Ollama 时自动降级，不影响主流程。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=10, pady=(0, 6))
        for label, key, width in [("Ollama Host", "ollama_host", 50),
                                  ("Ollama Model", "ollama_model", 30)]:
            frame = ttk.Frame(sf)
            frame.pack(fill=tk.X, pady=3, padx=10)
            ttk.Label(frame, text=label, width=20, anchor=tk.E).pack(side=tk.LEFT)
            var = tk.StringVar(value=self.config.get(key, ""))
            self.config_vars[key] = var
            ttk.Entry(frame, textvariable=var, width=width).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8, padx=10)

        ttk.Label(sf, text="基础功能（始终启用）", style="Section.TLabel").pack(anchor=tk.W, pady=(5, 4), padx=10)
        for feat in ["智能需求澄清", "多轮搜索策略", "素材去重与排序",
                     "章节自动撰写", "参考文献生成", "报告反馈与策略沉淀"]:
            ttk.Label(sf, text=f"• {feat}", style="Hint.TLabel").pack(anchor=tk.W, padx=30, pady=1)
        ttk.Label(sf, text="深度抓取开启时，素材分层存储会自动启用。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20, pady=(8, 0))

        ttk.Button(sf, text="保存配置", command=self.on_save_config,
                   style="Accent.TButton").pack(anchor=tk.W, pady=15, padx=10)

    def _build_regular_report_ui(self, parent):
        """常规搜索策略：控制批次、补搜和取数 Top K。"""
        _, sf = self._scroll_frame(parent)
        preset = self._load_preset_file("regular")

        ttk.Label(sf, text="常规搜索策略", style="Section.TLabel",
                  foreground="#2563EB").pack(anchor=tk.W, pady=(12, 2), padx=10)
        ttk.Label(sf, text="常规报告与行业搜索使用此策略；页数在左侧统一设置。",
                  style="Hint.TLabel").pack(anchor=tk.W, padx=10, pady=(0, 8))

        self.regular_batches_var = tk.StringVar(
            value=str(preset.get("search_batches", self.config.get("search_batches", 2))))
        self.regular_iterations_var = tk.StringVar(
            value=str(preset.get("max_iterations", self.config.get("max_iterations", 2))))
        self.regular_max_results_var = tk.StringVar(
            value=str(preset.get("max_search_results", self.config.get("max_search_results", 5))))
        self.regular_density_top_k_var = tk.StringVar(
            value=str(preset.get("search_density_top_k", self.config.get("search_density_top_k", 10))))
        self.regular_budget_var = tk.StringVar(
            value=str(preset.get("max_search_budget", self.config.get("max_search_budget", 20))))
        self.regular_auto_supplement_var = tk.BooleanVar(
            value=preset.get("auto_supplement", self.config.get("auto_supplement", True)))
        self.regular_auto_fill_gaps_var = tk.BooleanVar(
            value=preset.get("auto_fill_final_gaps", self.config.get("auto_fill_final_gaps", True)))
        self.regular_fill_rounds_var = tk.StringVar(
            value=str(preset.get("auto_fill_rounds", self.config.get("auto_fill_rounds", 1))))
        self.regular_fill_sources_var = tk.StringVar(
            value=str(preset.get("auto_fill_sources_per_round",
                                 self.config.get("auto_fill_sources_per_round", 20))))

        def add_row(label, var, from_, to_, hint=""):
            frame = ttk.Frame(sf)
            frame.pack(fill=tk.X, pady=3, padx=10)
            ttk.Label(frame, text=label, width=22, anchor=tk.E).pack(side=tk.LEFT)
            ttk.Spinbox(frame, from_=from_, to_=to_, width=8, textvariable=var).pack(side=tk.LEFT, padx=5)
            if hint:
                ttk.Label(frame, text=hint, style="Hint.TLabel").pack(side=tk.LEFT, padx=4)

        add_row("搜索批次", self.regular_batches_var, 1, 3, "1=宏观 2=+中观 3=+微观")
        add_row("每批迭代", self.regular_iterations_var, 1, 3, "每批基于上一轮结果细化")
        add_row("每次搜索取数 Top K", self.regular_max_results_var, 3, 10, "每个查询保留结果数")
        add_row("搜索密度 Top K", self.regular_density_top_k_var, 3, 15, "空缺方向多维深挖保留数")
        add_row("搜索预算", self.regular_budget_var, 5, 50, "总搜索次数上限（主搜索、补搜、事实核查共用）")
        add_row("补搜轮次", self.regular_fill_rounds_var, 1, 3, "写作后补搜循环次数")
        add_row("每轮补搜信源", self.regular_fill_sources_var, 5, 30, "每轮最多抓取信源数")

        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8, padx=10)
        ttk.Checkbutton(sf, text="自动补搜计划缺口（搜索后按计划补齐）",
                        variable=self.regular_auto_supplement_var).pack(anchor=tk.W, padx=20, pady=2)
        ttk.Checkbutton(sf, text="写作后补搜信息缺口",
                        variable=self.regular_auto_fill_gaps_var).pack(anchor=tk.W, padx=20, pady=2)
        ttk.Label(sf, text="深度抓取为全局开关，可在【功能配置】中调整。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20, pady=(8, 0))
        ttk.Button(sf, text="保存常规策略", command=lambda: self._save_preset("regular"),
                   style="Accent.TButton").pack(anchor=tk.W, pady=15, padx=10)

    def _build_deep_report_ui(self, parent):
        """深度搜索策略：更高批次、迭代和取数上限。"""
        _, sf = self._scroll_frame(parent)
        preset = self._load_preset_file("deep")

        ttk.Label(sf, text="深度搜索策略", style="Section.TLabel",
                  foreground="#B91C1C").pack(anchor=tk.W, pady=(12, 2), padx=10)
        ttk.Label(sf, text="超深度报告使用此策略；页数在左侧统一设置。",
                  style="Hint.TLabel").pack(anchor=tk.W, padx=10, pady=(0, 8))

        self.deep_batches_var = tk.StringVar(
            value=str(preset.get("search_batches", self.config.get("search_batches", 3))))
        self.deep_iterations_var = tk.StringVar(
            value=str(preset.get("max_iterations", self.config.get("max_iterations", 3))))
        self.deep_max_results_var = tk.StringVar(
            value=str(preset.get("max_search_results", self.config.get("max_search_results", 10))))
        self.deep_density_top_k_var = tk.StringVar(
            value=str(preset.get("search_density_top_k", self.config.get("search_density_top_k", 15))))
        self.deep_budget_var = tk.StringVar(
            value=str(preset.get("max_search_budget", self.config.get("max_search_budget", 40))))
        self.deep_auto_supplement_var = tk.BooleanVar(
            value=preset.get("auto_supplement", self.config.get("auto_supplement", True)))
        self.deep_auto_fill_gaps_var = tk.BooleanVar(
            value=preset.get("auto_fill_final_gaps", self.config.get("auto_fill_final_gaps", True)))
        self.deep_fill_rounds_var = tk.StringVar(
            value=str(preset.get("auto_fill_rounds", self.config.get("auto_fill_rounds", 3))))
        self.deep_fill_sources_var = tk.StringVar(
            value=str(preset.get("auto_fill_sources_per_round",
                                 self.config.get("auto_fill_sources_per_round", 10))))

        def add_row(label, var, from_, to_, hint=""):
            frame = ttk.Frame(sf)
            frame.pack(fill=tk.X, pady=3, padx=10)
            ttk.Label(frame, text=label, width=22, anchor=tk.E).pack(side=tk.LEFT)
            ttk.Spinbox(frame, from_=from_, to_=to_, width=8, textvariable=var).pack(side=tk.LEFT, padx=5)
            if hint:
                ttk.Label(frame, text=hint, style="Hint.TLabel").pack(side=tk.LEFT, padx=4)

        add_row("搜索批次", self.deep_batches_var, 1, 3, "1=宏观 2=+中观 3=+微观")
        add_row("每批迭代", self.deep_iterations_var, 1, 5, "每批基于上一轮结果细化")
        add_row("每次搜索取数 Top K", self.deep_max_results_var, 5, 15, "每个查询保留结果数")
        add_row("搜索密度 Top K", self.deep_density_top_k_var, 5, 30, "空缺方向多维深挖保留数")
        add_row("搜索预算", self.deep_budget_var, 10, 100, "总搜索次数上限（主搜索、补搜、事实核查共用）")
        add_row("补搜轮次", self.deep_fill_rounds_var, 1, 5, "写作后补搜循环次数")
        add_row("每轮补搜信源", self.deep_fill_sources_var, 5, 20, "每轮最多抓取信源数")

        ttk.Separator(sf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8, padx=10)
        ttk.Checkbutton(sf, text="自动补搜计划缺口（搜索后按计划补齐）",
                        variable=self.deep_auto_supplement_var).pack(anchor=tk.W, padx=20, pady=2)
        ttk.Checkbutton(sf, text="写作后补搜信息缺口",
                        variable=self.deep_auto_fill_gaps_var).pack(anchor=tk.W, padx=20, pady=2)
        ttk.Label(sf, text="深度抓取为全局开关，可在【功能配置】中调整。",
                  style="Hint.TLabel", wraplength=560).pack(anchor=tk.W, padx=20, pady=(8, 0))
        ttk.Button(sf, text="保存深度策略", command=lambda: self._save_preset("deep"),
                   style="Accent.TButton").pack(anchor=tk.W, pady=15, padx=10)

    def _load_preset_file(self, preset_name):
        """读取搜索策略预设；不存在或损坏时返回空字典。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"research_config_{preset_name}.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[策略] 读取 {preset_name} 预设失败: {e}")
            return {}

    def _strategy_vars(self, preset_name):
        """返回某个策略页的控件变量映射。"""
        if preset_name == "deep":
            return {
                "search_batches": self.deep_batches_var,
                "max_iterations": self.deep_iterations_var,
                "max_search_results": self.deep_max_results_var,
                "search_density_top_k": self.deep_density_top_k_var,
                "max_search_budget": self.deep_budget_var,
                "auto_supplement": self.deep_auto_supplement_var,
                "auto_fill_final_gaps": self.deep_auto_fill_gaps_var,
                "auto_fill_rounds": self.deep_fill_rounds_var,
                "auto_fill_sources_per_round": self.deep_fill_sources_var,
            }
        return {
            "search_batches": self.regular_batches_var,
            "max_iterations": self.regular_iterations_var,
            "max_search_results": self.regular_max_results_var,
            "search_density_top_k": self.regular_density_top_k_var,
            "max_search_budget": self.regular_budget_var,
            "auto_supplement": self.regular_auto_supplement_var,
            "auto_fill_final_gaps": self.regular_auto_fill_gaps_var,
            "auto_fill_rounds": self.regular_fill_rounds_var,
            "auto_fill_sources_per_round": self.regular_fill_sources_var,
        }

    def _collect_strategy(self, preset_name):
        """从策略页读取数值，非法输入回退到当前配置。"""
        values = {}
        for key, var in self._strategy_vars(preset_name).items():
            value = var.get()
            if isinstance(value, bool):
                values[key] = value
                continue
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                values[key] = int(self.config.get(key, 1) or 1)
        return values

    def _apply_strategy_to_vars(self, preset_name, data):
        for key, var in self._strategy_vars(preset_name).items():
            if key not in data:
                continue
            if isinstance(var, tk.BooleanVar):
                var.set(bool(data[key]))
            else:
                var.set(str(data[key]))

    def _active_preset_name(self):
        return "deep" if self.mode_var.get() == "deep" else "regular"

    def _apply_active_strategy(self):
        """把当前模式对应策略页的值写入运行时配置。"""
        self.config.update(self._collect_strategy(self._active_preset_name()))

    def _on_mode_change(self, notify=True):
        """切换报告模式时加载对应搜索策略。"""
        if not hasattr(self, "regular_batches_var"):
            return
        preset_name = self._active_preset_name()
        data = self._load_preset_file(preset_name)
        if data:
            self._apply_strategy_to_vars(preset_name, data)
        self._apply_active_strategy()
        if self.mode_var.get() == "industry":
            # 行业搜索：至少 3 批 + 自动补搜，保证行业面覆盖
            self.config["search_batches"] = max(3, int(self.config.get("search_batches", 3) or 3))
            self.config["auto_supplement"] = True
            self.regular_batches_var.set(str(self.config["search_batches"]))
            self.regular_auto_supplement_var.set(True)
        label = {"quick": "常规", "deep": "超深度", "industry": "行业搜索"}.get(
            self.mode_var.get(), "常规")
        if notify:
            self.set_status(f"已加载{label}搜索策略")

    def _save_preset(self, preset_name):
        """保存搜索策略到独立预设文件；当前模式的策略同步到主配置。"""
        preset = self._collect_strategy(preset_name)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"research_config_{preset_name}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(preset, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return

        if preset_name == self._active_preset_name():
            self.config.update(preset)
        if save_config(self.config):
            apply_config_to_env(self.config)
            self.set_status(f"已保存{'深度' if preset_name == 'deep' else '常规'}搜索策略")
            messagebox.showinfo("完成", "搜索策略已保存")
        else:
            messagebox.showerror("保存失败", "主配置写入失败，请查看日志")

    def setup_logger(self):
        """重定向 print 和 logging 到 GUI 日志框"""
        sys.stdout = GuiLogProxy(self)
        sys.stderr = GuiLogProxy(self)
        handler = GuiLogHandler(self)
        handler.setLevel(logging.INFO)
        logging.getLogger("xiaozui").addHandler(handler)
        logging.getLogger("xiaozui").setLevel(logging.INFO)

    def _do_append_log(self, text):
        """主线程专用：直接操作 GUI"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def set_status(self, text):
        self._safe_ui(lambda: self.status_label.config(text=text))

    def set_progress(self, running: bool):
        self._safe_ui(lambda: self.progress.start(10) if running else self.progress.stop())

    def set_chapters(self, chapters: List[str]):
        self._chapters_md = chapters

    def set_action_plan(self, plan: str):
        self._action_plan = plan

    def set_all_materials(self, materials: List[Dict]):
        self._all_materials = materials

    # ---------- 事件处理 ----------
    def _clear_task_placeholder(self, event=None):
        if self.task_input.get("1.0", tk.END).strip() == self.task_placeholder:
            self.task_input.delete("1.0", tk.END)

    def _restore_task_placeholder(self, event=None):
        if not self.task_input.get("1.0", tk.END).strip():
            self.task_input.insert("1.0", self.task_placeholder)

    def on_start(self):
        if not self.config.get("llm_api_key"):
            messagebox.showwarning("提示", "请先配置大模型 API Key")
            return
        task = self.task_input.get("1.0", tk.END).strip()
        if not task or task == self.task_placeholder:
            messagebox.showwarning("提示", "请输入调研需求")
            self.task_input.focus()
            return
        try:
            pages = int(self.pages_var.get())
            if pages > 0:
                self.config["max_pages"] = pages
        except (ValueError, TypeError):
            pass
        self.task_text = task
        mode = self.mode_var.get()
        self.waiting_state = None
        self._apply_active_strategy()
        self.config["v15_mode_forced"] = (mode == "deep")
        if mode == "industry":
            # 行业搜索：行业级搜索，不逐企业展开
            self.config["search_batches"] = max(3, int(self.config.get("search_batches", 3) or 3))
            self.config["auto_supplement"] = True
        apply_config_to_env(self.config)

        task_id = self._new_task_id()
        self.current_record = {
            "id": datetime.now().strftime("%m%d-%H%M%S"),
            "task_id": task_id,
            "title": task[:30] + ("..." if len(task) > 30 else ""),
            "text": task, "status": "running", "stage": "", "error": "",
            "report_path": "", "cost_api": 0.0, "cost_search": 0.0,
        }
        self.resume_task_id = ""
        self.config["task_id"] = task_id
        self.config["resume_task_id"] = ""
        self.start_event.set()
        self.set_status("AI正在审视需求...")
        self._start_worker()

    def _start_worker(self):
        self.start_btn.config(state=tk.DISABLED)
        self.retry_btn.config(state=tk.DISABLED)
        self.set_progress(True)
        self.notebook.select(self.log_tab)
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.worker_thread = threading.Thread(target=self.run_bot, daemon=True)
            self.worker_thread.start()

    def _output_root(self) -> str:
        return self.config.get("output_dir") or "."

    def _new_task_id(self) -> str:
        return f"research_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    def _update_retry_button(self):
        self.retry_btn.config(state=tk.NORMAL if self.last_failed_task else tk.DISABLED)

    def _load_last_failed_task(self):
        """启动时只恢复最近一个未完成任务，不再维护批量队列。"""
        try:
            items = list_unfinished_tasks(self._output_root())
            if not items:
                return
            self.last_failed_task = items[-1]
            self._update_retry_button()
            self.status_label.config(text="发现未完成任务，可点击【重试上次任务】继续")
        except Exception as e:
            logger.warning(f"[任务恢复] 读取失败: {e}")

    def on_retry_last_task(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            messagebox.showwarning("提示", "当前仍有任务在运行，请等待结束")
            return
        rec = self.last_failed_task
        if not rec or not rec.get("task_id"):
            messagebox.showinfo("提示", "没有可重试的失败任务")
            return
        self.task_text = rec.get("text", "")
        self.resume_task_id = rec["task_id"]
        self.current_record = dict(rec)
        self.current_record.update({"status": "running", "stage": "", "error": ""})
        self.config["task_id"] = rec["task_id"]
        self.config["resume_task_id"] = rec["task_id"]
        apply_config_to_env(self.config)
        self.waiting_state = None
        self.set_status("重试上次任务：从失败阶段继续...")
        self.start_event.set()
        self._start_worker()

    def run_bot(self):
        record = self.current_record
        try:
            self._chapters_md, self._action_plan, self._all_materials = [], "", []
            self.bot = FusionResearchBot(self.config, gui=self)
            asyncio.run(self.bot.run())
            if not self._chapters_md and getattr(self.bot, "chapters_md", None):
                self._chapters_md = self.bot.chapters_md

            if self._chapters_md:
                store = getattr(self.bot, "_state_store", None)
                compiler = DeliverableCompiler(self.config["output_dir"], self.config, self.bot.llm)
                for name, step in (
                    ("图表", lambda: compiler.generate_charts(self._chapters_md, self._all_materials)),
                    ("Excel附录", lambda: compiler.generate_excel_appendix(self._all_materials)),
                    ("仪表盘", compiler.generate_dashboard),
                ):
                    try:
                        result = step()
                        if store:
                            store.record_deliverable(name, True, str(result or ""))
                    except Exception as e:
                        logger.warning(f"[交付] {name}生成失败，继续编译报告: {e}")
                        if store:
                            store.record_deliverable(name, False, f"{type(e).__name__}: {e}")
                final_path = compiler.compile_word(
                    self.bot.report_title, self._chapters_md, self._action_plan,
                    source_appendix=self.bot._get_source_appendix() if hasattr(self.bot, '_get_source_appendix') else "",
                )
                # 同步产出精炼版报告
                gui_mode = self.mode_var.get()
                brief_mode = {"quick": "regular", "deep": "deep"}.get(gui_mode, "regular")
                try:
                    brief_path = compiler.compile_brief(
                        self.bot.report_title, self._chapters_md,
                        self._action_plan, mode=brief_mode
                    )
                    if brief_path:
                        logger.info(f"[精炼报告] 已生成：{brief_path}")
                    if store:
                        store.record_deliverable("精炼报告", True, str(brief_path or ""))
                except Exception as e:
                    logger.warning(f"[精炼报告] 生成失败: {e}")
                    if store:
                        store.record_deliverable("精炼报告", False, f"{type(e).__name__}: {e}")
                # Excel 与报告同名保存
                try:
                    compiler.generate_excel_appendix(self._all_materials, final_path)
                except Exception as e:
                    logger.warning(f"[交付] Excel同名附录生成失败: {e}")
                # 生成溯源Excel附录（含SABC信源等级）
                try:
                    data_points = self._extract_data_points_for_traceability()
                    if data_points:
                        compiler.generate_excel_appendix_with_traceability(data_points, final_path)
                        if store:
                            store.record_deliverable("SABC溯源", True)
                    elif store:
                        store.record_deliverable("SABC溯源", False, "未提取到可溯源数据点")
                except Exception as e:
                    logger.warning(f"[SABC溯源] 生成失败: {e}")
                    if store:
                        store.record_deliverable("SABC溯源", False, f"{type(e).__name__}: {e}")
                if store:
                    store.set_report_path(final_path)
                if record:
                    record.update({"status": "done", "stage": "", "error": "",
                                   "report_path": final_path})
                self.last_failed_task = None
                self._safe_ui(self._update_retry_button)
                self.set_final_report(final_path)
            else:
                raise RuntimeError("未生成报告章节")
        except StageFailed as e:
            logger.error(f"[任务失败] {e} | 已完成阶段可复用，点击【重试上次任务】从失败阶段继续")
            if record:
                record.update({"status": "failed", "stage": e.stage, "error": str(e)})
                self.last_failed_task = dict(record)
                self._safe_ui(self._update_retry_button)
            self.set_status(f"任务失败：{e.stage_name}（可重试）")
            self.set_progress(False)
        except SystemExit:
            pass
        except Exception as e:
            logger.error(f"[致命错误] {e}")
            import traceback
            logger.error(traceback.format_exc())
            if record:
                record.update({"status": "failed", "stage": record.get("stage", ""),
                               "error": f"{type(e).__name__}: {e}"})
                self.last_failed_task = dict(record)
                self._safe_ui(self._update_retry_button)
            self.set_progress(False)
            self.set_status("发生错误，请查看日志；可重试已保存的阶段")
        finally:
            self.resume_task_id = ""
            self.config["resume_task_id"] = ""
            self.config.pop("task_id", None)
            self.current_record = None
            self._safe_ui(lambda: self.start_btn.config(state=tk.NORMAL))
            self._safe_ui(self._update_retry_button)

    def _extract_data_points_for_traceability(self) -> List[Dict]:
        """从报告和素材中提取数据点用于SABC溯源Excel"""
        try:
            from knowledge_extractor import KnowledgeExtractor
            ke = KnowledgeExtractor(self.config, self.bot.llm)
            knowledge = ke.extract_from_report("\n".join(self._chapters_md))

            materials = getattr(self.bot, "all_materials", None) or []
            data_points = []
            for dp in knowledge.data_points:
                # 查找对应的素材和信源等级
                source_url = ""
                source_grade = "C"
                for m in self._all_materials:
                    url = m.get("url", "") if isinstance(m, dict) else getattr(m, "url", "")
                    content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
                    if dp.source and dp.source in url:
                        source_url = url
                        from config import grade_source
                        source_grade = grade_source(url, content).value
                        break
                # 溯源两列：优先按已匹配到的 URL，其次按来源名在 URL/标题中的子串匹配
                matched_material = None
                material_index = ""
                for idx, m in enumerate(materials, 1):
                    m_url = getattr(m, "url", "") or ""
                    m_title = getattr(m, "title", "") or ""
                    if source_url:
                        if m_url == source_url:
                            matched_material = m
                            material_index = str(idx)
                            break
                    elif dp.source and (dp.source in m_url or dp.source in m_title):
                        matched_material = m
                        material_index = str(idx)
                        break

                data_points.append({
                    "name": dp.metric,
                    "value": dp.value,
                    "unit": "",
                    "year": dp.year,
                    "source": dp.source or source_url,
                    "tier": source_grade,
                    "confidence": "high" if source_grade in ["S", "A"] else "medium" if source_grade == "B" else "low",
                    "verification_status": "待验证",
                    "valid_until": dp.expires_at,
                    "search_query": getattr(matched_material, "query", ""),
                    "material_index": material_index,
                })
            return data_points
        except Exception as e:
            logger.warning(f"[SABC溯源] 数据点提取失败: {e}")
            return []

    def on_save_config(self):
        """保存主配置。"""
        for key, var in self.config_vars.items():
            self.config[key] = var.get().strip()

        try:
            pages = int(self.pages_var.get())
            if pages > 0:
                self.config["max_pages"] = pages
        except (ValueError, TypeError):
            pass

        self.config["enable_deep_scrape"] = self.deep_scrape_var.get()
        self.config["enable_storm_debate"] = self.storm_var.get()
        self.config["enable_ai_knowledge_fallback"] = self.ai_fallback_var.get()

        if save_config(self.config):
            apply_config_to_env(self.config)
            self.config_status.config(text="配置已保存并生效")
            self.set_status("配置已更新")
        else:
            self.config_status.config(text="保存失败")

    def _on_confirm_shortcut(self, event=None):
        """Ctrl+Enter 快捷确认，避免鼠标定位不到底部按钮。"""
        if self.waiting_state:
            self.on_outline_confirm()

    def on_outline_confirm(self):
        if self.waiting_state == 'outline_controls':
            self.outline_action = "confirm"
            self.outline_text = ""
            self.outline_event.set()
        elif self.waiting_state == 'clarify':
            self.clarify_answer = self.feedback_entry.get("1.0", tk.END).strip()
            self.clarify_event.set()
        elif self.waiting_state == 'plan':
            self.plan_result = None
            self.plan_event.set()
        elif self.waiting_state == 'framework_confirm':
            self.outline_action = "framework_confirm"
            self.outline_event.set()
        else:
            messagebox.showwarning("提示", "当前状态不支持此操作")
            return
        self.waiting_state = None
        self.set_status("已确认，继续执行...")
        self.set_progress(True)

    def on_outline_edit(self):
        text = self.feedback_entry.get("1.0", tk.END).strip()
        if self.waiting_state == 'outline_controls':
            if not text:
                messagebox.showwarning("提示", "请输入修改内容")
                return
            self.outline_action = "edit"
            self.outline_text = text
            self.outline_event.set()
        elif self.waiting_state == 'plan':
            if not text:
                messagebox.showwarning("提示", "请输入修改意见")
                return
            self.plan_result = text
            self.plan_event.set()
        else:
            messagebox.showwarning("提示", "当前阶段不支持修改操作")
            return
        self.waiting_state = None
        self.set_status("已应用修改，继续执行...")
        self.set_progress(True)

    def on_outline_remelt(self):
        if self.waiting_state == 'outline_controls':
            self.outline_action = "remelt"
            self.outline_text = ""
            self.outline_event.set()
            self.waiting_state = None
            self.set_status("回炉重造骨架中...")
            self.set_progress(True)
        else:
            messagebox.showwarning("提示", "当前阶段不支持回炉操作，请等待引擎就绪")

    def on_outline_supplement(self):
        text = self.feedback_entry.get("1.0", tk.END).strip()
        if self.waiting_state == 'outline_controls':
            if not text:
                messagebox.showwarning("提示", "请输入补搜指令，如：第2章补搜 竞争格局，市场份额（多个关键词用逗号分隔）")
                return
            self.outline_action = "supplement"
            self.outline_text = text
            self.outline_event.set()
        else:
            messagebox.showwarning("提示", "当前阶段不支持补搜操作")
            return
        self.waiting_state = None
        self.set_status("补搜执行中...")
        self.set_progress(True)

    def on_confirm_framework(self):
        """框架确认后继续执行"""
        if self.waiting_state == 'framework_confirm':
            self.outline_action = "framework_confirm"
            self.outline_text = ""
            self.outline_event.set()
            self.waiting_state = None
            self.set_status("框架已确认，开始执行...")
            self.set_progress(True)
        else:
            messagebox.showwarning("提示", "当前状态不支持框架确认")

    # ---------- 阻塞等待接口（供引擎调用） ----------
    def wait_for_task(self) -> str:
        self.start_event.wait()
        self.start_event.clear()
        return self.task_text

    def wait_for_clarify(self, questions: str) -> str:
        self.waiting_state = 'clarify'
        self._safe_ui(lambda: self._show_clarify(questions))
        self.clarify_event.wait()
        self.clarify_event.clear()
        return self.clarify_answer

    def _show_clarify(self, questions):
        """主线程专用"""
        self.plan_text_widget.delete("1.0", tk.END)
        self.plan_text_widget.insert(tk.END, questions)
        self.plan_text_widget.see("1.0")
        self.notebook.select(self.plan_tab)
        self.status_label.config(text="AI提出澄清问题，请在左侧回答后点【确认通过】")
        self.progress.stop()
        self.feedback_entry.delete("1.0", tk.END)
        self.feedback_entry.focus()

    def wait_for_plan(self, plan_text: str):
        self.waiting_state = 'plan'
        self._safe_ui(lambda: self._show_plan(plan_text))
        self.plan_event.wait()
        self.plan_event.clear()
        return self.plan_result

    def _show_plan(self, plan_text):
        self.plan_text_widget.delete("1.0", tk.END)
        self.plan_text_widget.insert(tk.END, plan_text)
        self.plan_text_widget.see("1.0")
        self.notebook.select(self.plan_tab)
        self.status_label.config(text="等待确认调研计划...")
        self.progress.stop()
        self.feedback_entry.delete("1.0", tk.END)
        self.feedback_entry.focus()

    def wait_for_framework_confirm(self, framework_text: str):
        """展示框架并等待用户确认"""
        self.waiting_state = 'framework_confirm'
        self.outline_action = None
        self.outline_text = ""
        self._safe_ui(lambda: self._show_framework_confirm(framework_text))
        self.outline_event.wait()
        self.outline_event.clear()
        self._safe_ui(self._hide_framework_confirm)
        return self.outline_action

    def wait_for_outline_controls(self, outline_text: str):
        self.waiting_state = 'outline_controls'
        self.outline_action = None
        self.outline_text = ""
        self._safe_ui(lambda: self._show_outline_controls(outline_text))
        self.outline_event.wait()
        self.outline_event.clear()
        return self.outline_action, self.outline_text

    def _show_framework_confirm(self, framework_text):
        """展示解析后的框架，等待用户确认"""
        self.outline_text_widget.delete("1.0", tk.END)
        self.outline_text_widget.insert(tk.END, framework_text)
        self.outline_text_widget.see("1.0")
        self.notebook.select(self.outline_tab)
        self.status_label.config(text="请确认调研框架...")
        self.progress.stop()
        self.start_btn.config(state=tk.DISABLED)
        self.confirm_fw_btn.pack(fill=tk.X, pady=2, before=self.start_btn)

    def _hide_framework_confirm(self):
        """隐藏框架确认按钮；开始按钮保持原位，由主流程结束后恢复。"""
        self.confirm_fw_btn.pack_forget()

    def _show_outline_controls(self, outline_text):
        self.outline_text_widget.delete("1.0", tk.END)
        self.outline_text_widget.insert(tk.END, outline_text)
        self.outline_text_widget.see("1.0")
        self.notebook.select(self.outline_tab)
        self.status_label.config(text="等待确认控制面...")
        self.progress.stop()
        self.feedback_entry.delete("1.0", tk.END)
        self.feedback_entry.focus()

    def set_final_report(self, path: str):
        self.final_report_path = path
        self._safe_ui(self._show_final)

    def _show_final(self):
        self.report_info.config(text=f"报告已生成\n\n路径：\n{self.final_report_path}")
        self.notebook.select(self.report_tab)
        self.status_label.config(text="全部完成")
        self.progress.stop()
        
        # 显示缓存统计（本地端调试用）
        try:
            if hasattr(self, 'bot') and self.bot and hasattr(self.bot, 'searcher'):
                cache_stats = self.bot.searcher.cache.get_stats()
                if cache_stats and cache_stats.get("enabled") and cache_stats.get("session_hits", 0) > 0:
                    cache_msg = f"\n\n缓存统计：\n命中率: {cache_stats['hit_rate']}\n节省费用: {cache_stats['estimated_savings']}\n缓存条目: {cache_stats['cached_items']}条"
                    messagebox.showinfo("完成", f"报告已生成！\n\n{self.final_report_path}{cache_msg}")
                    return
        except (ValueError, TypeError):
            pass
        
        messagebox.showinfo("完成", f"报告已生成！\n\n{self.final_report_path}")

    def open_report_dir(self):
        if self.final_report_path and os.path.exists(self.final_report_path):
            subprocess.Popen(f'explorer /select,"{self.final_report_path}"')
        else:
            messagebox.showwarning("提示", "报告尚未生成")

    def copy_report_path(self):
        if self.final_report_path:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.final_report_path)
            messagebox.showinfo("提示", "路径已复制到剪贴板")
        else:
            messagebox.showwarning("提示", "报告尚未生成")

    def on_collect_feedback(self):
        """基于最终报告收集用户满意度，交给 LLM 整理为可复用策略。"""
        if not self._chapters_md:
            messagebox.showwarning("提示", "请先生成报告")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("报告反馈与策略沉淀")
        dlg.geometry("760x560")
        ttk.Label(dlg, text="报告章节：", font=("微软雅黑", 10, "bold")).pack(anchor=tk.W, padx=12, pady=(12, 4))
        chapters = "\n".join(
            (ch.splitlines()[0] if ch.splitlines() else "")[:100]
            for ch in self._chapters_md
        )
        ttk.Label(dlg, text=chapters[:1200], foreground="gray", justify=tk.LEFT,
                  wraplength=720).pack(anchor=tk.W, padx=12)
        ttk.Label(dlg, text="\n哪些部分你满意？希望以后保留什么结构、数据或写法？",
                  font=("微软雅黑", 10)).pack(anchor=tk.W, padx=12, pady=(12, 4))
        text = tk.Text(dlg, height=14, font=("微软雅黑", 10), wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        def submit():
            feedback = text.get("1.0", tk.END).strip()
            if not feedback:
                messagebox.showwarning("提示", "请填写反馈")
                return
            dlg.destroy()
            threading.Thread(
                target=self._save_feedback_strategy, args=(feedback,), daemon=True
            ).start()

        ttk.Button(dlg, text="整理并保存策略", command=submit).pack(pady=10)

    def _save_feedback_strategy(self, feedback: str):
        try:
            bot = getattr(self, "bot", None)
            if not bot:
                raise RuntimeError("报告尚未完成")
            chapters = "\n".join(self._chapters_md)[:6000]
            prompt = f"""你是调研策略整理助手。根据报告章节和用户反馈，整理成下次调研可复用的策略。
只输出 Markdown，包含：保留的结构、数据偏好、写作要求、需要避免的问题。不要复述报告正文。

【报告章节】
{chapters}

【用户反馈】
{feedback}
"""
            strategy = bot._agent_call(
                "planner", [{"role": "user", "content": prompt}], max_tokens=1200
            )
            out_dir = os.path.join(self.config.get("output_dir") or ".", "strategy")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"user_strategy_{datetime.now():%Y%m%d_%H%M%S}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# 用户反馈\n\n{feedback}\n\n# 整理后的策略\n\n{strategy}\n")
            self._safe_ui(lambda: self.set_status(f"策略已保存：{path}"))
        except Exception as e:
            self._safe_ui(lambda: messagebox.showerror("策略保存失败", str(e)))


# ============================================================
# 日志代理类
# ============================================================

class GuiLogProxy:
    """捕获 print / sys.stdout，通过 Queue 送到 GUI"""
    def __init__(self, gui: ResearchGUI):
        self.gui = gui
    def write(self, s):
        if s:
            self.gui._safe_ui(lambda: self.gui._do_append_log(s))
    def flush(self):
        pass
    def isatty(self):
        return False


class GuiLogHandler(logging.Handler):
    """捕获 logging 模块，通过 Queue 送到 GUI"""
    def __init__(self, gui: ResearchGUI):
        super().__init__()
        self.gui = gui
        self.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record) + "\n"
            self.gui._safe_ui(lambda: self.gui._do_append_log(msg))
        except Exception as e:
            logger.warning(f"主流程异常: {e}")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = ResearchGUI(root)
    root.mainloop()
