#!/usr/bin/env python3
"""用户反馈收集"""
import json, os, threading
from datetime import datetime

FEEDBACK_FILE = os.path.join(os.path.dirname(__file__), "feedback.json")
_FEEDBACK_LOCK = threading.Lock()

def save_feedback(report_id, rating, comment="", report_title=""):
    feedback = {
        "report_id": report_id,
        "timestamp": datetime.now().isoformat(),
        "rating": rating,
        "comment": comment,
        "report_title": report_title
    }
    with _FEEDBACK_LOCK:
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                try:
                    all_fb = json.load(f)
                except json.JSONDecodeError:
                    all_fb = []
        else:
            all_fb = []
        all_fb.append(feedback)
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(all_fb, f, ensure_ascii=False, indent=2)

def get_feedback_stats():
    with _FEEDBACK_LOCK:
        if not os.path.exists(FEEDBACK_FILE):
            return {"total": 0, "average_rating": 0}
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            try:
                all_fb = json.load(f)
            except json.JSONDecodeError:
                return {"total": 0, "average_rating": 0}
    total = len(all_fb)
    avg = sum(f["rating"] for f in all_fb) / total if total > 0 else 0
    return {"total": total, "average_rating": round(avg, 1)}
