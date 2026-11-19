"""
shared/document/ocr — OCR引擎适配器
=====================================

统一OCR接口，消除调研工具 local_ocr.py 中三个Adapter的重复代码。

支持的Provider：
- PaddleOCR：本地部署，中文识别效果好
- DeepSeek-OCR：API调用，DeepSeek视觉模型
- External API：通用外部API接口
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from shared.document.models import OCRResult, OCRProvider

logger = logging.getLogger(__name__)


class BaseOCRAdapter(ABC):
    """OCR适配器基类"""

    provider: str = "base"

    @abstractmethod
    def parse_image(self, image_path: str) -> OCRResult:
        """识别单张图片"""
        ...

    @abstractmethod
    def parse_pdf(self, pdf_path: str, dpi: int = 200) -> OCRResult:
        """识别PDF文档（先转图片再OCR）"""
        ...

    def _pdf_to_images(self, pdf_path: str, dpi: int = 200) -> List[str]:
        """
        PDF转图片（统一实现，消除3x重复）

        使用 pdf2image (MIT许可证)，需要安装 poppler。
        移除 PyMuPDF (AGPL-3.0) 以避免许可证传染风险。
        """
        import tempfile
        import os

        # 方案: pdf2image (MIT许可证，安全)
        try:
            from pdf2image import convert_from_path
            images = []
            # 自动检测 poppler bin 路径
            poppler_path = self._find_poppler()
            pil_images = convert_from_path(pdf_path, dpi=dpi, poppler_path=poppler_path)
            for img in pil_images:
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                img.save(tmp.name, "PNG")
                images.append(tmp.name)
            return images
        except ImportError:
            raise RuntimeError(
                "pdf2image未安装。请执行: pip install pdf2image\n"
                "并安装 poppler: https://github.com/oschwartz10612/poppler-windows/releases"
            )
        except Exception as e:
            logger.error(f"[OCR] pdf2image转换失败: {e}")
            raise RuntimeError(f"PDF转图片失败: {e}")

    @staticmethod
    def _find_poppler() -> Optional[str]:
        """自动检测 poppler bin 目录"""
        import os
        import glob
        import shutil

        # 1. 检查 PATH 中的 pdfinfo
        if shutil.which("pdfinfo"):
            return None  # PATH 已配置，无需指定

        # 2. 扫描常见安装位置
        search_paths = [
            os.path.expandvars(r"%ProgramFiles%\poppler*\Library\bin"),
            os.path.expandvars(r"%USERPROFILE%\poppler\poppler-*\Library\bin"),
            r"C:\poppler\*\Library\bin",
            r"C:\poppler*\Library\bin",
        ]
        for pattern in search_paths:
            matches = glob.glob(pattern)
            for match in matches:
                pdfinfo = os.path.join(match, "pdfinfo.exe")
                if os.path.isfile(pdfinfo):
                    return match

        return None


class PaddleOCRAdapter(BaseOCRAdapter):
    """PaddleOCR适配器（本地部署）"""

    provider = "paddleocr"

    def __init__(self, paddleocr_cmd: str = "paddleocr", lang: str = "ch"):
        self.paddleocr_cmd = paddleocr_cmd
        self.lang = lang
        self._ocr = None

    def _ensure_ocr(self):
        if self._ocr is not None:
            return
        try:
            from paddleocr import PaddleOCR
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self.lang, show_log=False)
        except ImportError:
            raise RuntimeError("PaddleOCR未安装: pip install paddleocr")

    def parse_image(self, image_path: str) -> OCRResult:
        self._ensure_ocr()
        result = self._ocr.ocr(image_path, cls=True)
        texts = []
        for line in result[0]:
            texts.append(line[1][0])
        full_text = "\n".join(texts)
        return OCRResult(text=full_text, pages=[full_text], provider=self.provider, page_count=1)

    def parse_pdf(self, pdf_path: str, dpi: int = 200) -> OCRResult:
        images = self._pdf_to_images(pdf_path, dpi)
        all_texts = []
        for img_path in images:
            result = self.parse_image(img_path)
            all_texts.append(result.text)
            # 清理临时文件
            import os
            try:
                os.unlink(img_path)
            except (ImportError, OSError) as e:
                logger.debug(f"OCR引擎加载失败: {e}")
        full_text = "\n\n---\n\n".join(all_texts)
        return OCRResult(
            text=full_text, pages=all_texts,
            provider=self.provider, page_count=len(all_texts),
        )


class DeepSeekOCRAdapter(BaseOCRAdapter):
    """DeepSeek OCR适配器（API调用）"""

    provider = "deepseek_ocr"

    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com/v1", model: str = "deepseek-ocr"):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def parse_image(self, image_path: str) -> OCRResult:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        import requests
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                            {"type": "text", "text": "请识别图片中的所有文字，保持原始格式输出。"},
                        ]},
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return OCRResult(text=text, pages=[text], provider=self.provider, page_count=1)
        except requests.exceptions.Timeout:
            logger.warning(f"[OCR] 请求超时: {image_path}")
            return OCRResult(text="", pages=[], provider=self.provider, page_count=0, error="请求超时")
        except requests.exceptions.RequestException as e:
            logger.warning(f"[OCR] 请求失败: {e}")
            return OCRResult(text="", pages=[], provider=self.provider, page_count=0, error=str(e))
        except Exception as e:
            logger.warning(f"[OCR] 识别失败: {e}")
            return OCRResult(text="", pages=[], provider=self.provider, page_count=0, error=str(e))

    def parse_pdf(self, pdf_path: str, dpi: int = 200) -> OCRResult:
        images = self._pdf_to_images(pdf_path, dpi)
        all_texts = []
        for img_path in images:
            result = self.parse_image(img_path)
            all_texts.append(result.text)
            import os
            try:
                os.unlink(img_path)
            except (ImportError, OSError) as e:
                logger.debug(f"OCR引擎加载失败: {e}")
        full_text = "\n\n---\n\n".join(all_texts)
        return OCRResult(text=full_text, pages=all_texts, provider=self.provider, page_count=len(all_texts))


class ExternalAPIAdapter(BaseOCRAdapter):
    """外部API OCR适配器（通用接口）"""

    provider = "external_api"

    def __init__(self, endpoint: str, api_key: str = ""):
        self.endpoint = endpoint
        self.api_key = api_key

    def parse_image(self, image_path: str) -> OCRResult:
        import base64, requests
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = requests.post(
            self.endpoint,
            json={"image": img_b64},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", data.get("result", ""))
        return OCRResult(text=text, pages=[text], provider=self.provider, page_count=1)

    def parse_pdf(self, pdf_path: str, dpi: int = 200) -> OCRResult:
        images = self._pdf_to_images(pdf_path, dpi)
        all_texts = []
        for img_path in images:
            result = self.parse_image(img_path)
            all_texts.append(result.text)
            import os
            try:
                os.unlink(img_path)
            except (ImportError, OSError) as e:
                logger.debug(f"OCR引擎加载失败: {e}")
        full_text = "\n\n---\n\n".join(all_texts)
        return OCRResult(text=full_text, pages=all_texts, provider=self.provider, page_count=len(all_texts))


class PPStructureAdapter(BaseOCRAdapter):
    """PP-StructureV3 轻量版适配器（版面分析+OCR）

    仅启用核心模块：
    - PP-DocLayout-S（版面区域检测，4.8MB）
    - PP-OCRv5_mobile（文字识别，det 4.7MB + rec 16MB）

    关闭模块（节省资源）：
    - 表格识别、公式识别、印章识别、图表解析、文档预处理

    输出：带版面结构的 Markdown + 阅读顺序排序的文本
    """

    provider = "ppstructure"

    def __init__(self, layout_model: str = "PP-DocLayout-S", ocr_version: str = "PP-OCRv5"):
        self.layout_model = layout_model
        self.ocr_version = ocr_version
        self._engine = None

    def _ensure_engine(self):
        if self._engine is not None:
            return
        try:
            from paddleocr import PPStructureV3
            # 轻量配置：只开版面检测+OCR，关闭其他模块
            self._engine = PPStructureV3(
                layout_model=self.layout_model,      # PP-DocLayout-S（4.8MB）
                ocr_version=self.ocr_version,         # PP-OCRv5_mobile
                # 关闭非必要模块
                use_doc_preprocessor=False,           # 关闭文档预处理
                use_table_recognition=False,          # 关闭表格识别
                use_formula_recognition=False,        # 关闭公式识别
                use_seal_recognition=False,           # 关闭印章识别
                use_chart_parsing=False,              # 关闭图表解析
                # 输出格式
                save_format=["markdown", "json"],     # Markdown + JSON（含坐标）
            )
            logger.info(f"[PP-StructureV3] 轻量引擎初始化完成: layout={self.layout_model}, ocr={self.ocr_version}")
        except ImportError:
            raise RuntimeError(
                "PaddleOCR 3.0+ 未安装。请执行: pip install paddleocr>=3.0\n"
                "或: pip install paddle paddle paddleocr"
            )
        except Exception as e:
            logger.error(f"[PP-StructureV3] 引擎初始化失败: {e}")
            raise RuntimeError(f"PP-StructureV3 初始化失败: {e}")

    def parse_image(self, image_path: str) -> OCRResult:
        """识别单张图片，返回版面分析+OCR结果"""
        self._ensure_engine()

        try:
            output = self._engine.predict(image_path)
            result = next(iter(output), None)
            if result is None:
                return OCRResult(text="", provider=self.provider, page_count=1)

            # 提取 Markdown 文本（已按阅读顺序排序）
            markdown_text = getattr(result, "markdown", "") or ""

            # 提取结构化数据（JSON格式，含坐标和标签）
            json_data = getattr(result, "json", {}) or {}
            regions = json_data.get("regions", [])

            # 计算平均置信度
            confidences = []
            for region in regions:
                for line in region.get("lines", []):
                    confidences.append(line.get("confidence", 0.0))
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

            # 构建元数据
            metadata = {
                "layout_regions": len(regions),
                "region_types": list(set(r.get("type", "") for r in regions)),
                "has_images": any(r.get("type") == "image" for r in regions),
                "has_tables": any(r.get("type") == "table" for r in regions),
            }

            return OCRResult(
                text=markdown_text,
                pages=[markdown_text],
                confidence=round(avg_confidence, 3),
                provider=self.provider,
                page_count=1,
                metadata=metadata,
            )

        except Exception as e:
            logger.error(f"[PP-StructureV3] 图片识别失败: {e}")
            # 降级：返回空结果，不阻塞流程
            return OCRResult(text=f"[版面分析失败: {e}]", provider=self.provider, page_count=1)

    def parse_pdf(self, pdf_path: str, dpi: int = 200) -> OCRResult:
        """识别PDF：先转图片，再逐页版面分析"""
        images = self._pdf_to_images(pdf_path, dpi)
        all_texts = []
        all_metadata = []
        total_confidence = 0.0

        for img_path in images:
            result = self.parse_image(img_path)
            all_texts.append(result.text)
            total_confidence += result.confidence
            all_metadata.append(result.metadata)
            # 清理临时文件
            import os
            try:
                os.unlink(img_path)
            except OSError:
                pass

        full_text = "\n\n---\n\n".join(all_texts)
        avg_confidence = total_confidence / len(all_texts) if all_texts else 0.0

        # 合并元数据
        merged_metadata = {
            "page_count": len(all_texts),
            "pages_metadata": all_metadata,
        }

        return OCRResult(
            text=full_text,
            pages=all_texts,
            confidence=round(avg_confidence, 3),
            provider=self.provider,
            page_count=len(all_texts),
            metadata=merged_metadata,
        )


class OCRFactory:
    """OCR工厂：根据配置创建合适的OCR适配器"""

    _adapters = {
        OCRProvider.PADDLEOCR: PaddleOCRAdapter,
        OCRProvider.DEEPSEEK: DeepSeekOCRAdapter,
        OCRProvider.EXTERNAL_API: ExternalAPIAdapter,
        OCRProvider.PPSTRUCTURE: PPStructureAdapter,
    }

    @classmethod
    def create(cls, provider: OCRProvider = OCRProvider.AUTO, **kwargs) -> BaseOCRAdapter:
        """
        创建OCR适配器

        Args:
            provider: OCR引擎类型，AUTO时自动选择
            **kwargs: 传递给适配器的参数
        """
        if provider == OCRProvider.AUTO:
            provider = cls._auto_detect(**kwargs)

        adapter_cls = cls._adapters.get(provider)
        if adapter_cls is None:
            raise ValueError(f"不支持的OCR引擎: {provider}")

        return adapter_cls(**kwargs)

    @classmethod
    def _auto_detect(cls, **kwargs) -> OCRProvider:
        """自动检测可用的OCR引擎"""
        # 优先使用PaddleOCR（免费、本地）
        try:
            import paddleocr
            return OCRProvider.PADDLEOCR
        except ImportError:
            pass

        # 其次使用DeepSeek OCR（需要API Key）
        if kwargs.get("api_key"):
            return OCRProvider.DEEPSEEK

        # 最后使用外部API
        if kwargs.get("endpoint"):
            return OCRProvider.EXTERNAL_API

        raise RuntimeError("没有可用的OCR引擎，请安装 PaddleOCR 或配置 API Key")
