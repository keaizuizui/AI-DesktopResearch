"""
shared/document/pdf — PDF解析器
================================

统一PDF解析接口，MIT许可证（无AGPL依赖）：
- 文本型PDF：pdfplumber提取（MIT）
- 扫描型PDF：OCR识别（通过微服务或本地）
- 自动检测：智能路由到对应解析器
- 表格提取：pdfplumber支持

⚠️ 
- 使用 pdfplumber (MIT) 替代 PyPDF2
- 支持表格提取
- PDF转图片保留在微服务中
"""

from __future__ import annotations

import logging
from typing import List, Optional

from shared.document.models import PDFDocument, PDFType
from shared.document.ocr import OCRFactory, OCRProvider, BaseOCRAdapter

logger = logging.getLogger(__name__)


class PDFParser:
    """
    统一PDF解析器

    功能：
    - 自动检测PDF类型（文本/扫描）
    - 文本型PDF使用pdfplumber提取（MIT许可证）
    - 扫描型PDF使用OCR识别
    - 支持表格提取
    """

    def __init__(
        self,
        ocr_provider: OCRProvider = OCRProvider.AUTO,
        ocr_kwargs: Optional[dict] = None,
    ):
        self.ocr_adapter: Optional[BaseOCRAdapter] = None
        self._ocr_kwargs = ocr_kwargs or {}
        self._ocr_provider = ocr_provider

    def _ensure_ocr(self):
        if self.ocr_adapter is not None:
            return
        self.ocr_adapter = OCRFactory.create(self._ocr_provider, **self._ocr_kwargs)

    def detect_type(self, pdf_path: str) -> PDFType:
        """
        检测PDF类型

        策略：尝试文本提取，如果提取到的文本足够多则判定为文本型
        """
        try:
            text = self.extract_text(pdf_path)
            # 如果每页平均超过50个字符，认为是文本型
            if text and len(text.strip()) > 50:
                return PDFType.TEXT
        except (ImportError, OSError) as e:
            logger.debug(f"PDF引擎加载失败: {e}")
        return PDFType.SCAN

    def parse(self, pdf_path: str, force_type: Optional[PDFType] = None) -> PDFDocument:
        """
        解析PDF文档

        Args:
            pdf_path: PDF文件路径
            force_type: 强制指定类型，None则自动检测

        Returns:
            PDFDocument 解析结果
        """
        pdf_type = force_type or self.detect_type(pdf_path)
        logger.info(f"[PDFParser] 解析 {pdf_path}, 类型: {pdf_type.value}")

        if pdf_type == PDFType.TEXT:
            return self._parse_text_pdf(pdf_path)
        else:
            return self._parse_scan_pdf(pdf_path)

    def _parse_text_pdf(self, pdf_path: str) -> PDFDocument:
        """解析文本型PDF"""
        text = self.extract_text(pdf_path)
        pages = self.extract_pages(pdf_path)
        return PDFDocument(
            text=text,
            pages=pages,
            pdf_type=PDFType.TEXT,
            page_count=len(pages),
        )

    def _parse_scan_pdf(self, pdf_path: str) -> PDFDocument:
        """解析扫描型PDF（OCR）"""
        self._ensure_ocr()
        ocr_result = self.ocr_adapter.parse_pdf(pdf_path)
        return PDFDocument(
            text=ocr_result.text,
            pages=ocr_result.pages,
            pdf_type=PDFType.SCAN,
            page_count=ocr_result.page_count,
            metadata={"ocr_provider": ocr_result.provider, "ocr_confidence": ocr_result.confidence},
        )

    def extract_text(self, pdf_path: str) -> str:
        """
        使用pdfplumber提取文本（MIT许可证）

        pdfplumber优势：
        - MIT许可证，无AGPL风险
        - 自动处理多栏布局
        - 支持表格提取
        """
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                texts = []
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        texts.append(t)
                return "\n\n".join(texts)
        except ImportError:
            raise RuntimeError(
                "pdfplumber未安装，请执行: pip install pdfplumber"
            )

    def extract_pages(self, pdf_path: str) -> List[str]:
        """按页提取文本"""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                pages = []
                for page in pdf.pages:
                    t = page.extract_text()
                    pages.append(t or "")
                return pages
        except ImportError:
            raise RuntimeError("pdfplumber未安装: pip install pdfplumber")

    def extract_tables(self, pdf_path: str) -> List[List[List[str]]]:
        """
        提取PDF中的所有表格（优先Camelot，降级pdfplumber）

        使用pdfplumber的表格检测功能。

        Returns:
            List[List[List[str]]]: 表格列表，每行为单元格列表
        """
        # 优先尝试 Camelot（处理复杂表格更好）
        try:
            import camelot
            tables = camelot.read_pdf(pdf_path, pages='all', flavor='stream')
            if tables and len(tables) > 0:
                logger.info(f"[PDF] Camelot 提取到 {len(tables)} 个表格")
                return [table.df.values.tolist() for table in tables]
        except ImportError:
            logger.debug("[PDF] Camelot 未安装，使用 pdfplumber")
        except Exception as e:
            logger.warning(f"[PDF] Camelot 提取失败: {e}，降级到 pdfplumber")
        
        # 降级到 pdfplumber
        try:
            import pdfplumber
            all_tables = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if tables:
                        for table in tables:
                            if table:
                                all_tables.append(table)
            return all_tables
        except ImportError:
            raise RuntimeError("pdfplumber未安装: pip install pdfplumber")

    def parse_to_markdown(self, pdf_path: str) -> str:
        """
        解析PDF并输出Markdown格式

        每页用 --- 分隔符分开
        """
        doc = self.parse(pdf_path)
        if not doc.pages:
            return doc.text
        return "\n\n---\n\n".join(doc.pages)

    def get_page_count(self, pdf_path: str) -> int:
        """获取PDF页数"""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                return len(pdf.pages)
        except ImportError:
            raise RuntimeError("pdfplumber未安装: pip install pdfplumber")

    def get_metadata(self, pdf_path: str) -> dict:
        """
        获取PDF元信息（标题/作者/页数等）

        使用pdfplumber的metadata接口（MIT许可证）。

        Returns:
            dict: {
                "page_count": int,
                "title": str,
                "author": str,
                "subject": str,
                "creator": str,
                "producer": str,
                "creation_date": str,
                "mod_date": str,
            }
        """
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                meta = pdf.metadata or {}
                return {
                    "page_count": len(pdf.pages),
                    "title": meta.get("Title", ""),
                    "author": meta.get("Author", ""),
                    "subject": meta.get("Subject", ""),
                    "creator": meta.get("Creator", ""),
                    "producer": meta.get("Producer", ""),
                    "creation_date": meta.get("CreationDate", ""),
                    "mod_date": meta.get("ModDate", ""),
                }
        except ImportError:
            raise RuntimeError("pdfplumber未安装: pip install pdfplumber")
