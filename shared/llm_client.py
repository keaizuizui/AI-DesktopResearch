"""
统一 JSON 工具函数

调研主链路使用 调研工具/config.py 的 LLMClient；本模块只保留业务实际
引用的 JSON 清洗与解析函数，避免维护多套未接入的 LLM 客户端实现。
"""
import json
import re


def extract_json_text(text: str) -> str:
    """
    从 LLM 返回的文本中提取纯净 JSON 字符串。

    处理策略（按优先级）：
    1. ```json ... ``` 代码块
    2. ``` ... ``` 代码块（无 json 标记）
    3. 最外层 {...} 或 [...] 结构

    注意：本函数只做文本清洗，不做 json.loads 解析。
    调用者可自行 json.loads() 或使用 safe_json_loads()。

    Args:
        text: LLM 返回的原始文本（可能包含 Markdown 包裹）

    Returns:
        提取出的 JSON 字符串；无法提取时返回原文
    """
    text = (text or "").strip()
    if not text:
        return ""

    # 1. ```json ... ```
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 2. ``` ... ```（无 json 标记）
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 3. 最外层 {...} 或 [...]
    m = re.search(r"([\{\[].*[\}\]])", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    return text


def safe_json_loads(text: str, default=None):
    """
    安全解析 JSON：先清洗文本再解析，失败返回默认值。

    Args:
        text: 可能包含 Markdown 包裹的 JSON 文本
        default: 解析失败时的返回值，默认 None

    Returns:
        解析后的 Python 对象（dict / list / str 等），
        解析失败时返回 default
    """
    try:
        return json.loads(extract_json_text(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default if default is not None else {}
