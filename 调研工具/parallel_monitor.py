#!/usr/bin/env python3
"""
并行撰写监控工具 - 外挂测试
================================
功能：
1. 监控 engine.py 的 write_chapters 并行执行情况
2. 记录每章的素材使用情况
3. 检测重复内容和逻辑矛盾
4. 生成并行效果报告

用法：
    python parallel_monitor.py --report-path "报告目录"
"""

import sys
import os
import re
import json
import argparse
from pathlib import Path
from typing import List, Dict, Set, Tuple
from collections import Counter


def extract_chapters(report_md: str) -> List[Dict]:
    """从报告markdown中提取各章内容"""
    chapters = []
    # 匹配 ## 第X章 或 ### 章节标题
    pattern = r'(?:^|\n)(#{2,3}\s+.+?)(?=\n#{2,3}\s+|\Z)'
    matches = re.findall(pattern, report_md, re.DOTALL)
    
    for i, match in enumerate(matches, 1):
        title = match.split('\n')[0].strip('# ').strip()
        content = '\n'.join(match.split('\n')[1:])
        chapters.append({
            "index": i,
            "title": title,
            "content": content,
            "word_count": len(content)
        })
    return chapters


def detect_duplicates(chapters: List[Dict]) -> List[Dict]:
    """检测章节间的重复内容"""
    duplicates = []
    
    # 提取每章的关键句子（包含数字、品牌、案例的句子）
    def extract_key_sentences(text: str) -> Set[str]:
        sentences = re.split(r'[。！？\n]', text)
        key_sentences = set()
        for s in sentences:
            s = s.strip()
            # 包含数字或具体品牌的句子
            if re.search(r'\d+', s) or re.search(r'[公司|集团|品牌|企业]', s):
                # 取前30字作为指纹
                key_sentences.add(s[:30])
        return key_sentences
    
    chapter_keys = []
    for ch in chapters:
        keys = extract_key_sentences(ch["content"])
        chapter_keys.append({"title": ch["title"], "keys": keys})
    
    # 检测重复
    for i in range(len(chapter_keys)):
        for j in range(i + 1, len(chapter_keys)):
            overlap = chapter_keys[i]["keys"] & chapter_keys[j]["keys"]
            if overlap:
                duplicates.append({
                    "chapter_a": chapter_keys[i]["title"],
                    "chapter_b": chapter_keys[j]["title"],
                    "overlap_count": len(overlap),
                    "overlap_samples": list(overlap)[:3]
                })
    
    return duplicates


def detect_contradictions(chapters: List[Dict]) -> List[Dict]:
    """检测章节间的逻辑矛盾（数字不一致）"""
    contradictions = []
    
    # 提取所有数字声明
    def extract_numbers(text: str) -> Dict[str, str]:
        numbers = {}
        # 匹配 "指标：数值" 或 "指标达到数值"
        patterns = [
            r'([^，。\n]{2,10}?)[:：]\s*(\d+(?:\.\d+)?%?)',
            r'([^，。\n]{2,10}?)达到\s*(\d+(?:\.\d+)?%?)',
            r'([^，。\n]{2,10}?)为\s*(\d+(?:\.\d+)?%?)',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                key = match.group(1).strip()
                value = match.group(2)
                if len(key) >= 2 and len(key) <= 10:
                    numbers[key] = value
        return numbers
    
    # 收集所有章节的数字
    all_numbers = {}
    for ch in chapters:
        nums = extract_numbers(ch["content"])
        for key, value in nums.items():
            if key not in all_numbers:
                all_numbers[key] = []
            all_numbers[key].append({
                "chapter": ch["title"],
                "value": value
            })
    
    # 检测同一指标在不同章节有不同值
    for key, occurrences in all_numbers.items():
        if len(occurrences) > 1:
            values = [o["value"] for o in occurrences]
            if len(set(values)) > 1:
                contradictions.append({
                    "indicator": key,
                    "occurrences": occurrences
                })
    
    return contradictions


def analyze_parallel_quality(report_path: str) -> Dict:
    """分析并行撰写的质量"""
    # 读取报告
    if os.path.isdir(report_path):
        # 查找最新的 .md 文件
        md_files = list(Path(report_path).glob("*.md"))
        if not md_files:
            return {"error": "未找到markdown报告文件"}
        report_path = str(max(md_files, key=lambda p: p.stat().st_mtime))
    
    with open(report_path, 'r', encoding='utf-8') as f:
        report_md = f.read()
    
    # 提取章节
    chapters = extract_chapters(report_md)
    
    if len(chapters) < 2:
        return {"error": "章节数不足，无法分析并行效果"}
    
    # 检测重复
    duplicates = detect_duplicates(chapters)
    
    # 检测矛盾
    contradictions = detect_contradictions(chapters)
    
    # 统计
    word_counts = [ch["word_count"] for ch in chapters]
    avg_words = sum(word_counts) / len(word_counts)
    word_variance = sum((w - avg_words) ** 2 for w in word_counts) / len(word_counts)
    
    return {
        "report_path": report_path,
        "chapter_count": len(chapters),
        "total_words": sum(word_counts),
        "avg_words_per_chapter": round(avg_words, 0),
        "word_variance": round(word_variance, 0),
        "word_balance": "均衡" if word_variance < avg_words * 0.5 else "不均衡",
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "contradictions": contradictions,
        "contradiction_count": len(contradictions),
        "quality_score": max(0, 100 - len(duplicates) * 10 - len(contradictions) * 15),
        "recommendation": "并行效果良好" if len(duplicates) == 0 and len(contradictions) == 0 else 
                         "建议降低并行数或启用写前同步" if len(duplicates) > 2 or len(contradictions) > 1 else
                         "有轻微问题，可接受"
    }


def main():
    parser = argparse.ArgumentParser(description="并行撰写质量监控工具")
    parser.add_argument("--report-path", required=True, help="报告文件或目录路径")
    parser.add_argument("--output", default="parallel_report.json", help="输出报告路径")
    args = parser.parse_args()
    
    result = analyze_parallel_quality(args.report_path)
    
    # 打印结果
    print("=" * 60)
    print("并行撰写质量分析报告")
    print("=" * 60)
    
    if "error" in result:
        print(f"错误: {result['error']}")
        return
    
    print(f"报告路径: {result['report_path']}")
    print(f"章节数: {result['chapter_count']}")
    print(f"总字数: {result['total_words']}")
    print(f"平均每章字数: {result['avg_words_per_chapter']}")
    print(f"字数均衡度: {result['word_balance']}")
    print(f"重复章节对: {result['duplicate_count']}")
    print(f"逻辑矛盾: {result['contradiction_count']}")
    print(f"质量评分: {result['quality_score']}/100")
    print(f"建议: {result['recommendation']}")
    
    if result['duplicates']:
        print("\n重复内容详情:")
        for dup in result['duplicates'][:3]:
            print(f"  - {dup['chapter_a']} vs {dup['chapter_b']}: {dup['overlap_count']}处重复")
    
    if result['contradictions']:
        print("\n逻辑矛盾详情:")
        for con in result['contradictions'][:3]:
            print(f"  - 指标'{con['indicator']}'在不同章节值不一致:")
            for occ in con['occurrences']:
                print(f"    {occ['chapter']}: {occ['value']}")
    
    # 保存JSON报告
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n详细报告已保存: {args.output}")


if __name__ == "__main__":
    main()
