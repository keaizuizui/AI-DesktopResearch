#!/usr/bin/env python3
"""
广度调研测试脚本 - 非GUI模式
================================
测试需求：调研新能源汽车企业开发智能座舱产品，需要了解其他新能源汽车企业

用法：
    python test_broad_research.py
"""

import sys
import os
import asyncio
import time
import json

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config, save_config, apply_config_to_env, logger
from engine import FusionResearchBot


class MockGUI:
    """模拟GUI接口，用于非GUI模式运行"""
    
    def __init__(self):
        self.logs = []
        self.start_event = asyncio.Event()
        self.clarify_event = asyncio.Event()
        self.plan_event = asyncio.Event()
        self.outline_event = asyncio.Event()
        self.task_text = ""
        self.clarify_answer = ""
        self.plan_result = None
        self.outline_action = None
        self.outline_text = ""
        self._chapters_md = []
        self._all_materials = []
        self._action_plan = ""
        
    def wait_for_task(self) -> str:
        return "调研新能源汽车企业开发智能座舱产品，需要了解其他新能源汽车企业的市场策略、产品线和竞争优势"
    
    def wait_for_clarify(self, questions: str) -> str:
        print(f"\n[澄清问题] {questions}")
        # 自动回答，不做澄清
        return ""
    
    def wait_for_plan(self, plan_text: str):
        print(f"\n[调研计划] {plan_text[:200]}...")
        # 自动确认
        return None
    
    def wait_for_outline_controls(self, outline_text: str):
        print(f"\n[大纲控制] 大纲已生成，自动确认")
        # 自动确认
        return "confirm", ""
    
    def set_status(self, text: str):
        print(f"[状态] {text}")
    
    def set_progress(self, active: bool):
        pass
    
    def append_log(self, text: str):
        self.logs.append(text)
        print(f"[日志] {text[:100]}")
    
    def set_plan_text(self, text: str):
        print(f"[计划] {text[:100]}...")
    
    def set_outline_text(self, text: str):
        print(f"[大纲] {text[:100]}...")
    
    def set_action_plan(self, text: str):
        self._action_plan = text
        print(f"[行动建议] {text[:100]}...")
    
    def set_final_report(self, path: str):
        print(f"\n[完成] 报告已生成: {path}")
        self.final_report_path = path
    
    def set_chapters(self, chapters):
        print(f"[章节] 已生成 {len(chapters)} 章")
        self._chapters_md = chapters
    
    def set_all_materials(self, materials):
        print(f"[素材] 共 {len(materials)} 条")
        self._all_materials = materials


async def main():
    print("=" * 60)
    print("广度调研测试 - 新能源汽车智能座舱")
    print("=" * 60)
    
    # 加载配置
    config = load_config()
    print(f"\n[配置] 搜索深度: {config.get('search_depth', 'b')}")
    print(f"[配置] 搜索批次: {config.get('search_batches', 3)}")
    print(f"[配置] 自动补搜: {config.get('auto_supplement', False)}")
    print(f"[配置] 搜索密度TopK: {config.get('search_density_top_k', 10)}")
    print(f"[配置] 激进过滤TopN: {config.get('aggressive_filter_top_k', 3)}")
    
    # 创建模拟GUI
    gui = MockGUI()
    
    # 创建Bot
    start_time = time.time()
    bot = FusionResearchBot(config, gui=gui)
    
    # 运行调研
    try:
        await bot.run()
        
        # 获取结果
        elapsed = time.time() - start_time
        print(f"\n[统计] 总耗时: {elapsed:.1f}秒")
        print(f"[统计] 素材数量: {len(bot.all_materials)}")
        print(f"[统计] 章节数量: {len(bot.chapters_md) if bot.chapters_md else 0}")
        print(f"[统计] 搜索预算剩余: {bot.state.search_budget}")
        
        # 保存结果用于分析
        result = {
            "task": gui.task_text,
            "elapsed_time": elapsed,
            "material_count": len(bot.all_materials),
            "chapter_count": len(bot.chapters_md) if bot.chapters_md else 0,
            "search_budget_remaining": bot.state.search_budget,
            "config": {
                "search_depth": config.get('search_depth'),
                "search_batches": config.get('search_batches'),
                "auto_supplement": config.get('auto_supplement'),
                "search_density_top_k": config.get('search_density_top_k'),
                "aggressive_filter_top_k": config.get('aggressive_filter_top_k'),
            }
        }
        
        with open("test_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("\n[完成] 测试结果已保存到 test_result.json")
        
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
