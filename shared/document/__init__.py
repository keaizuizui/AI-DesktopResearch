"""
shared/document — 统一文档处理接口
======================================

将散落在各子项目中的文档处理能力统一为标准接口：
- OCR：多Provider OCR引擎（PaddleOCR / DeepSeek-OCR / 外部API）
- PDF：文本提取 + 扫描版OCR解析 + 自动检测路由
- Word：Word文档生成（Markdown→Word）+ Word文档解析（Word→纯文本）
- Markdown：Markdown解析 + 对象序列化
- Excel：统一样式 + 排期表导出 + 数据附录导出

模块结构：
    ocr/          — OCR引擎适配器
    pdf/          — PDF解析器
    word/         — Word文档读写
    excel/        — Excel导出
    models.py     — 统一数据结构

使用方式：
    from shared.document.ocr import OCRFactory
    from shared.document.pdf import PDFParser
    from shared.document.word import WordReader, MarkdownDocxCompiler
    from shared.document.excel import ExcelExporter
"""

__all__ = [
    "OCRFactory", "PDFParser", "WordReader",
    "MarkdownDocxCompiler", "ExcelExporter",
]
