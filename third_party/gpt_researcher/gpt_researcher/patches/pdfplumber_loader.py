"""
PDFPlumberLoader — PyMuPDFLoader 兼容替换
===========================================

提供与 langchain_community.document_loaders.PyMuPDFLoader 相同的接口，
但底层使用 pdfplumber (MIT)，彻底消除 AGPL 风险。

⚠️ 替换说明：
GPT-Researcher 原版使用 langchain_community.document_loaders.PyMuPDFLoader
（底层是 PyMuPDF / fitz，AGPL-3.0 许可证）。
本模块实现了完全相同的接口（返回 langchain.schema.Document 对象），
因此 GPT-Researcher 代码无需修改 import 方式。

使用方法：
    # 原代码：
    from langchain_community.document_loaders import PyMuPDFLoader
    
    # 改为：
    from third_party.gpt_researcher.patches.pdfplumber_loader import PDFPlumberLoader as PyMuPDFLoader

许可证：MIT
"""

import os
import tempfile
from typing import List, Optional
from urllib.parse import urlparse

import requests


class PDFPlumberLoader:
    """
    PyMuPDFLoader 的 pdfplumber 替代实现
    
    接口完全兼容 langchain_community.document_loaders.PyMuPDFLoader。
    返回 List[Document]，每个 Document 有 page_content 和 metadata。
    """

    def __init__(self, file_path: str):
        """
        Args:
            file_path: PDF文件路径或URL
        """
        self.file_path = file_path

    def load(self) -> List:
        """
        加载PDF文档
        
        Returns:
            List[Document]: 每个元素代表一页，包含 page_content 和 metadata
        """
        from langchain_core.documents import Document
        import pdfplumber
        
        docs = []
        with pdfplumber.open(self.file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                docs.append(Document(
                    page_content=text,
                    metadata={
                        "source": self.file_path,
                        "page": i + 1,
                        "total_pages": len(pdf.pages),
                    }
                ))
        return docs


def load_pdf_with_pdfplumber(url_or_path: str) -> str:
    """
    使用 pdfplumber 加载PDF并返回文本
    
    替代 scrape_skills.py 中的 scrape_pdf_with_pdfplumber()
    
    Args:
        url_or_path: PDF的URL或本地路径
    
    Returns:
        str: 所有页面的文本
    """
    import pdfplumber
    
    # 如果是URL，先下载
    if _is_url(url_or_path):
        response = requests.get(url_or_path, timeout=30, stream=True)
        response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            for chunk in response.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name
        
        try:
            with pdfplumber.open(tmp_path) as pdf:
                texts = []
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        texts.append(t)
                return "\n\n".join(texts)
        finally:
            os.unlink(tmp_path)
    else:
        with pdfplumber.open(url_or_path) as pdf:
            texts = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
            return "\n\n".join(texts)


def _is_url(path: str) -> bool:
    """判断是否为URL"""
    try:
        result = urlparse(path)
        return all([result.scheme, result.netloc])
    except Exception:
        return False
