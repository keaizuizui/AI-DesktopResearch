#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / memory.py
记忆层：Ollama向量 + BM25 任务内混合检索 + 事实卡片压缩 + 置信度评分
不跨任务持久化，不读取历史知识库。
"""

import os, re, threading, time
from typing import List, Dict, Optional

import numpy as np
import requests

from v16_1_utils import smart_truncate
from config import SearchResult, logger, LLMClient, extract_json, AGENT_PROMPTS
from shared.llm_client import safe_json_loads


class OllamaVectorStore:
    def __init__(self, host: str, model: str, chunk_size: int = 8, timeout: int = 120,
                 query_timeout: int = 15, max_chars: int = 600, cooldown: int = 300):
        self.host = host.rstrip("/")
        self.model = model
        self.texts: List[str] = []
        self.metas: List[Dict] = []
        self.vectors: Optional[np.ndarray] = None
        self.chunk_size = max(1, int(chunk_size))
        self.timeout = max(10, int(timeout))
        self.query_timeout = max(5, int(query_timeout))
        self.max_chars = max(200, int(max_chars))
        self.cooldown = max(30, int(cooldown))
        self._fail_until = 0.0
        self._lock = threading.Lock()

    def _trip(self, reason: str):
        """Ollama 失败后进入冷却期，避免每次检索都卡在超时上。"""
        self._fail_until = time.time() + self.cooldown
        logger.warning(
            f"[向量] Ollama 不可用（{reason}），{self.cooldown}秒内跳过向量检索，改用 BM25"
        )

    def embed(self, texts: List[str], timeout: Optional[int] = None,
              return_indices: bool = False):
        """批量向量化；失败分片直接跳过，不写入零向量。

        Returns:
            return_indices=False: np.ndarray (N, dim)
            return_indices=True: (np.ndarray, 成功条目在原列表中的索引)
        """
        empty = (np.array([], dtype=np.float32), []) if return_indices else np.array([], dtype=np.float32)
        if not texts:
            return empty
        if time.time() < self._fail_until:
            logger.debug("[向量] Ollama 处于熔断冷却期，跳过向量化")
            return empty

        truncated = [t[:self.max_chars] for t in texts]
        vectors: List[List[float]] = []
        indices: List[int] = []
        for start in range(0, len(truncated), self.chunk_size):
            chunk = truncated[start:start + self.chunk_size]
            try:
                r = requests.post(
                    f"{self.host}/api/embed",
                    json={"model": self.model, "input": chunk},
                    timeout=timeout or self.timeout,
                )
                r.raise_for_status()
                embeddings = r.json().get("embeddings", [])
                if not isinstance(embeddings, list) or not embeddings:
                    raise RuntimeError("Ollama 返回空向量")
                for offset, emb in enumerate(embeddings[:len(chunk)]):
                    if isinstance(emb, list) and emb:
                        vectors.append(emb)
                        indices.append(start + offset)
            except Exception as e:
                self._trip(f"{type(e).__name__}: {e}")
                break

        if not vectors:
            return empty
        arr = np.array(vectors, dtype=np.float32)
        if return_indices:
            return arr, indices
        return arr

    def add(self, texts: List[str], metas: List[Dict]):
        if not texts:
            return
        processed_texts = []
        processed_metas = []
        for t, m in zip(texts, metas):
            if len(t) > 1200:
                skip_len = int(len(t) * 0.2)
                core = t[skip_len:]
                chunks = [core[i : i + 800] for i in range(0, len(core), 600)]
                for i, chunk in enumerate(chunks[:3]):
                    processed_texts.append(chunk)
                    cm = m.copy()
                    cm["_chunk_idx"] = i
                    processed_metas.append(cm)
            else:
                processed_texts.append(t)
                processed_metas.append(m)

        vecs, keep = self.embed(processed_texts, return_indices=True)
        if not keep:
            logger.warning(f"[向量] {len(processed_texts)}条素材未建向量索引，本次仅用 BM25 检索")
            return
        kept_texts = [processed_texts[i] for i in keep]
        kept_metas = [processed_metas[i] for i in keep]
        with self._lock:
            if self.vectors is not None and self.vectors.shape[1] != vecs.shape[1]:
                logger.warning("[向量] 模型维度变化，重建向量索引")
                self.vectors = None
                self.texts.clear()
                self.metas.clear()
            if self.vectors is None:
                self.vectors = vecs
                self.texts = kept_texts
                self.metas = kept_metas
            else:
                self.vectors = np.vstack([self.vectors, vecs])
                self.texts.extend(kept_texts)
                self.metas.extend(kept_metas)
        logger.info(
            f"[向量] 索引 {len(kept_texts)}/{len(processed_texts)} 条，"
            f"模型={self.model}，维度={vecs.shape[1]}"
        )

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        if self.vectors is None or len(self.texts) == 0:
            return []
        q_vecs = self.embed([query], timeout=self.query_timeout)
        if q_vecs.size == 0:
            return []
        q_vec = q_vecs[0]
        if q_vec.shape[0] != self.vectors.shape[1]:
            return []
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(q_vec)
        norms[norms == 0] = 1e-10
        sims = np.dot(self.vectors, q_vec) / norms
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [
            {"score": float(sims[i]), "text": self.texts[i], "meta": self.metas[i]}
            for i in top_idx
        ]

    def clear(self):
        with self._lock:
            self.texts.clear()
            self.metas.clear()
            self.vectors = None


try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    logger.warning("[BM25] rank_bm25 未安装，混合检索降级为纯向量。pip install rank-bm25")


class BM25Store:
    def __init__(self):
        self.texts: List[str] = []
        self.metas: List[Dict] = []
        self.bm25 = None
        self.tokenized: List[List[str]] = []
        self._dirty = False
        self._lock = threading.Lock()

    def _tokenize(self, text: str) -> List[str]:
        # 限制单条长度，避免超长素材让 BM25 构建/检索变慢
        tokens = re.findall(r'[\u4e00-\u9fa5]|[a-zA-Z]+|\d+', text[:2000].lower())
        return tokens or ["_empty_"]

    def add(self, texts: List[str], metas: List[Dict]):
        if not texts:
            return
        self.texts.extend(texts)
        self.metas.extend(metas)
        self.tokenized.extend([self._tokenize(t) for t in texts])
        self._dirty = True

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        if not BM25_AVAILABLE or not self.tokenized:
            return []
        with self._lock:
            if self.bm25 is None or self._dirty:
                try:
                    self.bm25 = BM25Okapi(self.tokenized)
                    self._dirty = False
                except Exception as e:
                    logger.warning(f"[BM25] 索引构建失败，改用向量检索: {e}")
                    return []
            bm25 = self.bm25
        try:
            scores = bm25.get_scores(self._tokenize(query))
        except Exception as e:
            logger.warning(f"[BM25] 检索失败: {e}")
            return []
        if len(scores) == 0 or float(np.max(scores)) <= 0:
            return []
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [
            {"score": float(scores[i]), "text": self.texts[i], "meta": self.metas[i]}
            for i in top_idx if scores[i] > 0
        ]

    def clear(self):
        with self._lock:
            self.texts.clear()
            self.metas.clear()
            self.tokenized.clear()
            self.bm25 = None
            self._dirty = False


class HybridMemoryStore:
    def __init__(self, ollama_host: str, ollama_model: str, llm: Optional[LLMClient] = None,
                 cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.vector_store = OllamaVectorStore(
            ollama_host, ollama_model,
            chunk_size=int(self.cfg.get("ollama_embed_chunk", 8)),
            timeout=int(self.cfg.get("ollama_embed_timeout", 120)),
            query_timeout=int(self.cfg.get("ollama_query_timeout", 15)),
            max_chars=int(self.cfg.get("ollama_embed_max_chars", 600)),
            cooldown=int(self.cfg.get("ollama_cooldown", 300)),
        )
        self.bm25_store = BM25Store()
        self.llm = llm
        self._all_metas: List[Dict] = []

    def _compress_fact_card(self, result: SearchResult) -> str:
        if not self.cfg.get("enable_fact_compression", True) or not self.llm or not self.llm.available:
            return result.content[:200]
        prompt = f"""【任务】将素材压缩为高密度事实卡片
【压缩原则】只保留关键数据+核心结论+来源
【字数限制】100字以内

【素材标题】{result.title}
【素材内容】{smart_truncate(result.content, 1200)}

【输出要求】
- 只输出压缩后的纯文本
- 不要JSON、不要Markdown、不要解释
- 必须包含：1个关键数据 + 1个核心结论 + 来源信息"""
        try:
            card = self.llm.fast([{"role": "user", "content": prompt}], max_tokens=200, temperature=0.3)
            return smart_truncate(card.strip(), 300)
        except Exception as e:
            logger.debug(f"[事实卡片压缩失败] {e}")
            return result.content[:200]

    def _compress_fact_cards_batch(self, results: List[SearchResult]) -> List[str]:
        """批量压缩事实卡片，一次LLM调用处理多条素材。"""
        if not self.cfg.get("enable_fact_compression", True) or not self.llm or not self.llm.available:
            return [r.content[:200] for r in results]
        cards = []
        batch_size = 10
        for batch_start in range(0, len(results), batch_size):
            batch = results[batch_start:batch_start + batch_size]
            items_text = "\n---\n".join([
                f"[{i+1}] 标题：{r.title}\n内容：{smart_truncate(r.content, 400)}"
                for i, r in enumerate(batch)
            ])
            prompt = f"""【任务】批量压缩{len(batch)}条素材为高密度事实卡片
【压缩原则】每条只保留：关键数据+核心结论+来源
【字数限制】每条100字以内

【素材列表】
{items_text}

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 确保JSON语法正确
- 标准格式：
[{{"idx":1,"card":"压缩后的事实卡片（含数据+结论+来源）"}},{{"idx":2,"card":"压缩后的事实卡片"}}]"""
            try:
                raw = self.llm.fast(
                    [{"role": "user", "content": prompt}],
                    system=AGENT_PROMPTS["evaluator"],
                    max_tokens=1200, temperature=0.3
                )
                extracted = extract_json(raw)
                data = safe_json_loads(extracted, []) if extracted else []
                if isinstance(data, list):
                    card_map = {}
                    for item in data:
                        if isinstance(item, dict):
                            card_map[item.get("idx", -1)] = item.get("card", "")[:200]
                    for i in range(len(batch)):
                        cards.append(card_map.get(i + 1, batch[i].content[:200]))
                else:
                    cards.extend([r.content[:200] for r in batch])
            except Exception as e:
                logger.debug(f"[批量压缩失败] batch {batch_start}: {e}")
                cards.extend([r.content[:200] for r in batch])
        return cards

    def _score_confidence(self, result: SearchResult) -> float:
        base = result.llm_score / 10.0
        reliability_map = {"high": 1.0, "medium": 0.6, "low": 0.2}
        rel = reliability_map.get(result.reliability, 0.5)
        verified_bonus = 0.1 if getattr(result, '_verification_for', None) else 0.0
        return min(1.0, base * 0.5 + rel * 0.4 + verified_bonus)

    def add(self, results: List[SearchResult]):
        if not results:
            return
        # 批量压缩替代逐条调用（5条以上批量）
        if len(results) >= 5 and self.llm and self.llm.available:
            cards = self._compress_fact_cards_batch(results)
        else:
            cards = []
            for r in results:
                cards.append(self._compress_fact_card(r))

        texts = []
        metas = []
        for idx, r in enumerate(results):
            r.fact_card = cards[idx] if idx < len(cards) else r.content[:200]
            r.confidence = self._score_confidence(r)
            texts.append(r.fact_card)
            meta = r.to_dict()
            metas.append(meta)
        # 标注在 all_materials 中的索引，供 _auto_assign 直接取用
        base_idx = len(self._all_metas)
        for i, meta in enumerate(metas):
            meta["_mat_idx"] = base_idx + i
        self.vector_store.add(texts, metas)
        raw_texts = [f"{r.title} {r.content[:800]}" for r in results]
        self.bm25_store.add(raw_texts, metas)
        self._all_metas.extend(metas)

    def add_fast(self, results: List[SearchResult]):
        """快速添加（跳过LLM压缩），用于批量搜索结果入库。

        fact_card 从 200 字提升到 800 字，利用 jina embedding 的语义分块能力，
        保留更多信息用于向量搜索匹配，避免素材在存储阶段就丢失关键信息。
        OllamaVectorStore.add 内部对 >1200 字的内容自动做 800字/块、600字重叠的分块。
        """
        if not results:
            return
        texts = []
        metas = []
        # fact_card 截断长度从 200 提升到 800
        fc_max = self.cfg.get("fast_fact_card_chars", 800)
        for r in results:
            r.fact_card = r.content[:fc_max]
            r.confidence = self._score_confidence(r)
            texts.append(r.fact_card)
            meta = r.to_dict()
            metas.append(meta)
        # 标注在 all_materials 中的索引，供 _auto_assign 直接取用
        base_idx = len(self._all_metas)
        for i, meta in enumerate(metas):
            meta["_mat_idx"] = base_idx + i
        self.vector_store.add(texts, metas)
        raw_texts = [f"{r.title} {r.content[:800]}" for r in results]
        self.bm25_store.add(raw_texts, metas)
        self._all_metas.extend(metas)

    def search(self, query: str, top_k: int = 10, vector_weight: float = 0.7) -> List[Dict]:
        vec_results = self.vector_store.search(query, top_k=top_k * 2)
        bm25_results = self.bm25_store.search(query, top_k=top_k * 2)

        fused = {}
        for item in vec_results:
            key = item["meta"].get("url", "") + item["meta"].get("title", "")
            if not key:
                key = hash(item["text"])
            fused[key] = {
                "score": item["score"] * vector_weight,
                "text": item["text"],
                "meta": item["meta"],
            }

        for item in bm25_results:
            key = item["meta"].get("url", "") + item["meta"].get("title", "")
            if not key:
                key = hash(item["text"])
            if key in fused:
                fused[key]["score"] += item["score"] * (1 - vector_weight)
            else:
                fused[key] = {
                    "score": item["score"] * (1 - vector_weight),
                    "text": item["text"],
                    "meta": item["meta"],
                }

        items = list(fused.values())
        if not items:
            return []
        max_score = max(i["score"] for i in items)
        if max_score > 0:
            for i in items:
                i["score"] /= max_score
        items.sort(key=lambda x: x["score"], reverse=True)
        return items[:top_k]

    def get_by_ids(self, idxs: List[int]) -> List[Dict]:
        """按0-based索引列表获取素材（write_chapters中已做i-1转换）"""
        results = []
        for idx in idxs:
            if 0 <= idx < len(self._all_metas):
                results.append({"meta": self._all_metas[idx], "text": "", "score": 1.0})
        return results

    def clear(self):
        self.vector_store.clear()
        self.bm25_store.clear()
        self._all_metas.clear()
