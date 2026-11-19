#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / task_state.py
任务状态持久化：记录每个阶段的执行状态、错误和产物。
核心阶段失败时不静默降级，任务标记为“可重试”，重启后仍可从失败阶段继续。
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("xiaozui")

# 阶段中文名（写日志、GUI 展示共用）
STAGE_LABELS = {
    "prepare": "准备任务",
    "v15": "超深度管线",
    "clarify": "澄清追问",
    "plan": "生成调研计划",
    "plan_confirm": "确认调研计划",
    "search": "深度搜索",
    "gap_check": "覆盖度检查与补搜",
    "outline": "素材评估与大纲生成",
    "skeleton": "生成骨架与素材分配",
    "outline_confirm": "确认报告骨架",
    "writing": "章节写作与统稿",
    "quality": "质量检查",
    "output": "输出报告",
}

# 配置里不落盘的敏感字段
_SECRET_MARKERS = ("key", "token", "secret", "password", "cookie")

_STATE_DIRNAME = "任务状态"


def stage_label(name: str) -> str:
    return STAGE_LABELS.get(name, name)


class StageFailed(RuntimeError):
    """核心阶段重试后仍失败，任务可恢复。"""

    def __init__(self, stage: str, error: Exception, attempts: int):
        self.stage = stage
        self.stage_name = stage_label(stage)
        self.error = error
        self.attempts = attempts
        super().__init__(f"阶段【{self.stage_name}】失败（尝试{attempts}次）：{error}")


def _atomic_write_json(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def safe_config_snapshot(cfg: Dict) -> Dict:
    """只保留可用于恢复的配置，API Key 等敏感项不落盘。"""
    clean = {}
    for k, v in (cfg or {}).items():
        if any(marker in k.lower() for marker in _SECRET_MARKERS):
            continue
        if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            clean[k] = v
    return clean


class TaskStateStore:
    """单个任务的持久化状态。"""

    def __init__(self, root_dir: str, task_id: str, task_text: str = "",
                 config: Optional[Dict] = None):
        self.task_id = task_id
        self.task_dir = os.path.join(root_dir or ".", _STATE_DIRNAME, task_id)
        self.state_path = os.path.join(self.task_dir, "task.json")
        self._lock = threading.Lock()
        self._had_state = os.path.exists(self.state_path)
        self.data = self._load() or {
            "task_id": task_id,
            "task_text": task_text,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": "",
            "status": "running",
            "current_stage": "",
            "error": "",
            "stages": {},
            "optional_errors": [],
            "deliverables": {},
            "report_path": "",
            "config": safe_config_snapshot(config or {}),
        }
        if task_text and not self.data.get("task_text"):
            self.data["task_text"] = task_text
        self._save()

    # ---------- 基础读写 ----------
    def _load(self) -> Optional[Dict]:
        if not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[任务状态] 读取失败 {self.state_path}: {e}")
            return None

    def _save(self):
        with self._lock:
            self.data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                _atomic_write_json(self.state_path, self.data)
            except Exception as e:
                logger.warning(f"[任务状态] 写入失败 {self.state_path}: {e}")

    @property
    def exists(self) -> bool:
        return self._had_state

    # ---------- 阶段状态 ----------
    def is_stage_done(self, name: str) -> bool:
        return self.data.get("stages", {}).get(name, {}).get("status") == "done"

    def begin_stage(self, name: str):
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "running"
        stage["attempts"] = int(stage.get("attempts", 0)) + 1
        stage["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stage.pop("error", None)
        self.data["status"] = "running"
        self.data["current_stage"] = name
        self._save()

    def finish_stage(self, name: str):
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "done"
        stage["ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.data["current_stage"] = ""
        self._save()

    def attempt_failed(self, name: str, error: Exception, attempts: int):
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "running"
        stage["error"] = f"{type(error).__name__}: {error}"
        stage["attempts"] = attempts
        self._save()

    def fail_stage(self, name: str, error: Exception):
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "failed"
        stage["error"] = f"{type(error).__name__}: {error}"
        stage["ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.data["status"] = "failed"
        self.data["current_stage"] = name
        self.data["error"] = f"阶段【{stage_label(name)}】失败：{error}"
        self._save()

    def optional_failed(self, name: str, error: Exception):
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        stage["status"] = "failed"
        stage["error"] = f"{type(error).__name__}: {error}"
        stage["ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.data.setdefault("optional_errors", []).append({
            "stage": name,
            "label": stage_label(name),
            "error": f"{type(error).__name__}: {error}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()

    def complete(self):
        self.data["status"] = "completed"
        self.data["current_stage"] = ""
        self.data["error"] = ""
        self._save()

    def record_deliverable(self, name: str, ok: bool, detail: str = ""):
        self.data.setdefault("deliverables", {})[name] = {
            "ok": bool(ok),
            "detail": detail,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not ok:
            self.data.setdefault("optional_errors", []).append({
                "stage": name,
                "label": name,
                "error": detail,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        self._save()

    def set_report_path(self, path: str):
        self.data["report_path"] = path or ""
        self._save()

    # ---------- 产物 ----------
    def save_artifact(self, name: str, data: Any):
        try:
            _atomic_write_json(os.path.join(self.task_dir, "artifacts", f"{name}.json"), data)
        except Exception as e:
            logger.warning(f"[任务状态] 保存产物 {name} 失败: {e}")

    def load_artifact(self, name: str) -> Any:
        path = os.path.join(self.task_dir, "artifacts", f"{name}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[任务状态] 读取产物 {name} 失败: {e}")
            return None

    def save_text(self, rel_path: str, text: str):
        try:
            _atomic_write_text(os.path.join(self.task_dir, rel_path), text)
        except Exception as e:
            logger.warning(f"[任务状态] 保存文本 {rel_path} 失败: {e}")

    def read_text(self, rel_path: str) -> str:
        path = os.path.join(self.task_dir, rel_path)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def save_chapters(self, chapters: List[str]):
        for i, text in enumerate(chapters):
            if text and text.strip():
                self.save_text(os.path.join("chapters", f"{i+1:02d}.md"), text)

    def load_chapters(self, count: int) -> Dict[int, str]:
        found = {}
        for i in range(count):
            text = self.read_text(os.path.join("chapters", f"{i+1:02d}.md"))
            if text.strip():
                found[i] = text
        return found


# ============================================================
# 未完成任务恢复
# ============================================================

def list_unfinished_tasks(root_dir: str) -> List[Dict]:
    """扫描任务状态目录，返回失败/中断的任务摘要。"""
    base = os.path.join(root_dir or ".", _STATE_DIRNAME)
    if not os.path.isdir(base):
        return []
    result = []
    for task_id in sorted(os.listdir(base)):
        path = os.path.join(base, task_id, "task.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") in ("failed", "running"):
                result.append({
                    "id": datetime.now().strftime("%m%d-%H%M-") + task_id[-6:],
                    "task_id": task_id,
                    "title": (data.get("task_text") or task_id)[:30],
                    "text": data.get("task_text", ""),
                    "status": "failed",
                    "stage": data.get("current_stage", ""),
                    "error": data.get("error", ""),
                    "report_path": data.get("report_path", ""),
                    "cost_api": 0.0,
                    "cost_search": 0.0,
                })
        except Exception:
            continue
    return result

