"""启动时探测本地 embedding 能力，自适应压缩分块大小。

GPTR 默认按 1000 字切块，但本地小模型的可用输入长度各不相同；
直接沿用默认值会触发 Ollama 413，并让整批 embedding 失败。
这里在服务启动时实测一档安全长度，写入 EMBED_CHUNK_SIZE 供压缩层读取。
"""
import os
import logging

import httpx

logger = logging.getLogger("embedding")

# 从大到小探测，命中即用；分块再乘 0.8 留出 tokenizer 差异余量
PROBE_LENGTHS = (600, 400, 200, 100)

_SAMPLE = (
    "中国无糖茶市场2024年零售额保持增长，农夫山泉东方树叶份额领先，三得利与果子熟了紧随其后。"
    "渠道端便利店与电商同步扩张，单店动销、件均价和价格带出现分化，促销力度加大。"
    "行业增速由铺货驱动转向单店动销，龙头企业通过品类创新和产能扩张巩固份额。"
)


def _sample(length: int) -> str:
    return (_SAMPLE * (length // len(_SAMPLE) + 1))[:length]


async def probe_embedding(host: str, model: str, enabled: bool) -> dict:
    """探测 Ollama 与模型是否可用，并确定安全分块长度。

    结果写入环境变量：GPTR_DISABLE_EMBEDDING / EMBED_CHUNK_SIZE。
    """
    if not enabled:
        os.environ["GPTR_DISABLE_EMBEDDING"] = "1"
        logger.info("[Embedding] 已关闭，压缩使用词面筛选")
        return {"enabled": False, "chunk_size": 0}

    timeout = httpx.Timeout(60.0, connect=3.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{host}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            installed = any(
                n == model or n == f"{model}:latest" or n.startswith(f"{model}:")
                for n in names
            )
            if not installed:
                os.environ["GPTR_DISABLE_EMBEDDING"] = "1"
                logger.warning(
                    f"[Embedding] 模型 {model} 未安装，压缩使用词面筛选；"
                    f"安装命令：ollama pull {model}（或在配置中更换 OLLAMA_MODEL）"
                )
                return {"enabled": False, "chunk_size": 0}

            for length in PROBE_LENGTHS:
                try:
                    r = await client.post(
                        f"{host}/api/embed",
                        json={"model": model, "input": [_sample(length)]},
                    )
                    r.raise_for_status()
                except Exception:
                    continue
                chunk = int(length * 0.8)
                os.environ["EMBED_CHUNK_SIZE"] = str(chunk)
                os.environ["GPTR_DISABLE_EMBEDDING"] = "0"
                logger.info(
                    f"[Embedding] {model} 可用，安全分块 {chunk} 字（探测上限 {length}）"
                )
                return {"enabled": True, "chunk_size": chunk}
    except Exception as e:
        os.environ["GPTR_DISABLE_EMBEDDING"] = "1"
        logger.warning(f"[Embedding] 无法连接 Ollama（{host}）：{e}，压缩使用词面筛选")
        return {"enabled": False, "chunk_size": 0}

    os.environ["GPTR_DISABLE_EMBEDDING"] = "1"
    logger.warning("[Embedding] 所有长度探测均失败，压缩使用词面筛选")
    return {"enabled": False, "chunk_size": 0}
