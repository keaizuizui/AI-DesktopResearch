"""
shared/document/models — 统一文档数据结构
============================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OCRProvider(str, Enum):
    """OCR引擎类型"""
    PADDLEOCR = "paddleocr"
    DEEPSEEK = "deepseek_ocr"
    EXTERNAL_API = "external_api"
    PPSTRUCTURE = "ppstructure"  # PP-StructureV3 轻量版（版面分析+OCR）
    AUTO = "auto"  # 自动选择


class PDFType(str, Enum):
    """PDF类型"""
    TEXT = "text"       # 文本型PDF（可直接提取文字）
    SCAN = "scan"       # 扫描型PDF（需要OCR）
    MIXED = "mixed"     # 混合型


@dataclass
class OCRResult:
    """OCR识别结果"""
    text: str = ""                    # 识别的完整文本
    pages: List[str] = field(default_factory=list)  # 按页分割的文本
    confidence: float = 0.0          # 平均置信度 0-1
    provider: str = ""               # 使用的OCR引擎
    page_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages": self.pages,
            "confidence": self.confidence,
            "provider": self.provider,
            "page_count": self.page_count,
        }


@dataclass
class PDFDocument:
    """PDF文档解析结果"""
    text: str = ""                    # 提取的完整文本
    pages: List[str] = field(default_factory=list)  # 按页分割的文本
    pdf_type: PDFType = PDFType.TEXT
    page_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    tables: List[List[List[str]]] = field(default_factory=list)  # 提取的表格


@dataclass
class WordDocument:
    """Word文档数据"""
    text: str = ""                    # 提取的纯文本
    paragraphs: List[str] = field(default_factory=list)
    tables: List[List[List[str]]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExcelExportConfig:
    """Excel导出配置"""
    title: str = ""
    sheet_name: str = "Sheet1"
    header_style: Dict[str, Any] = field(default_factory=dict)
    phase_colors: Dict[str, str] = field(default_factory=dict)  # 阶段→颜色映射
    auto_width: bool = True
    freeze_header: bool = True
