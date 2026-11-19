"""
shared/document/word — Word文档读写
=====================================

统一Word文档处理接口：
- WordReader：Word文档解析（.docx → 纯文本）
- MarkdownDocxCompiler：Markdown转Word渲染器

迁移自：
- 调研工具 framework_loader.py _docx_to_text()
- 调研工具 deliverables.py MarkdownDocxCompiler
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Word 文档解析
# ═══════════════════════════════════════════════════════════════

class WordReader:
    """
    Word文档解析器

    合并自：
    - 调研工具 framework_loader.py:120  _docx_to_text()

    功能：
    - 提取所有段落文本
    - 提取表格内容（单元格用 | 连接）
    - 返回拼接后的纯文本
    """

    @staticmethod
    def extract_text(docx_path: str, include_tables: bool = True) -> str:
        """
        从Word文档提取纯文本

        Args:
            docx_path: .docx文件路径
            include_tables: 是否包含表格内容

        Returns:
            拼接后的纯文本
        """
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx未安装: pip install python-docx")

        doc = Document(docx_path)
        parts = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)

        if include_tables:
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    row_text = " | ".join(cells)
                    if row_text.strip("| "):
                        parts.append(row_text)

        return "\n".join(parts)

    @staticmethod
    def extract_paragraphs(docx_path: str) -> List[str]:
        """提取所有非空段落"""
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx未安装: pip install python-docx")

        doc = Document(docx_path)
        return [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    @staticmethod
    def extract_tables(docx_path: str) -> List[List[List[str]]]:
        """提取所有表格"""
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx未安装: pip install python-docx")

        doc = Document(docx_path)
        tables = []
        for table in doc.tables:
            rows = []
            for row in table.rows:
                rows.append([cell.text.strip() for cell in row.cells])
            tables.append(rows)
        return tables


# ═══════════════════════════════════════════════════════════════
# Markdown → Word 渲染器
# ═══════════════════════════════════════════════════════════════

class MarkdownDocxCompiler:
    """
    Markdown转Word渲染器

    迁移自：调研工具 deliverables.py:509 MarkdownDocxCompiler

    功能：
    - 解析Markdown标题（##/###/####）
    - 解析Markdown表格
    - 解析无序/有序列表
    - 解析行内格式（粗体/斜体/代码/链接）
    - 解析分隔线（---）
    - 渲染为python-docx元素
    """

    def __init__(self, doc):
        """
        Args:
            doc: python-docx Document对象
        """
        self.doc = doc

    def add_markdown(self, md_text: str):
        """
        将Markdown文本渲染到Word文档

        Args:
            md_text: Markdown格式文本
        """
        from docx.shared import Pt, Inches

        for line in md_text.split("\n"):
            line = line.rstrip()

            # 空行
            if not line:
                continue

            # 分隔线
            if line.strip() == "---":
                self._add_separator()
                continue

            # 标题
            if line.startswith("#### "):
                self._add_heading(line[5:], level=4)
            elif line.startswith("### "):
                self._add_heading(line[4:], level=3)
            elif line.startswith("## "):
                self._add_heading(line[3:], level=2)
            elif line.startswith("# "):
                self._add_heading(line[2:], level=1)

            # 表格
            elif line.startswith("|") and "|" in line[1:]:
                # 收集完整表格
                # (需要在外层处理多行表格)
                pass  # 由 _parse_table_blocks 处理

            # 无序列表
            elif re.match(r"^[\-\*]\s", line):
                self._add_bullet(line)

            # 有序列表
            elif re.match(r"^\d+\.\s", line):
                self._add_numbered(line)

            # 普通段落
            else:
                self._add_paragraph(line)

    def add_markdown_with_tables(self, md_text: str):
        """
        将Markdown文本渲染到Word文档（含表格支持）

        先解析表格块，再逐块渲染
        """
        blocks = self._parse_table_blocks(md_text)
        for block_type, content in blocks:
            if block_type == "table":
                self._render_table(content)
            else:
                self.add_markdown(content)

    def _parse_table_blocks(self, md_text: str) -> List[Tuple[str, str]]:
        """将Markdown文本分割为表格块和非表格块"""
        blocks = []
        current_lines = []
        in_table = False

        for line in md_text.split("\n"):
            is_table_row = line.strip().startswith("|") and "|" in line.strip()[1:]
            is_separator = bool(re.match(r"^\|[\s\-:|]+\|$", line.strip()))

            if is_table_row and not is_separator:
                if not in_table:
                    # 开始表格块，先保存之前的非表格内容
                    if current_lines:
                        blocks.append(("text", "\n".join(current_lines)))
                        current_lines = []
                    in_table = True
                current_lines.append(line)
            elif is_separator and in_table:
                continue  # 跳过表格分隔行
            else:
                if in_table:
                    # 结束表格块
                    blocks.append(("table", "\n".join(current_lines)))
                    current_lines = []
                    in_table = False
                current_lines.append(line)

        # 处理最后的块
        if in_table and current_lines:
            blocks.append(("table", "\n".join(current_lines)))
        elif current_lines:
            blocks.append(("text", "\n".join(current_lines)))

        return blocks

    def _render_table(self, table_md: str):
        """渲染Markdown表格为Word表格"""
        try:
            from docx import Document
            from docx.shared import Pt, Cm
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            from docx.oxml.ns import qn
        except ImportError:
            raise RuntimeError("python-docx未安装")

        rows = []
        for line in table_md.strip().split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells:
                rows.append(cells)

        if not rows:
            return

        table = self.doc.add_table(rows=len(rows), cols=len(rows[0]))
        table.style = "Light Grid Accent 1"

        for i, row_data in enumerate(rows):
            for j, cell_text in enumerate(row_data):
                if j < len(table.columns):
                    cell = table.cell(i, j)
                    cell.text = cell_text
                    # 表头加粗
                    if i == 0:
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:
                                run.bold = True

    def _add_heading(self, text: str, level: int = 2):
        """添加标题"""
        from docx.shared import Pt
        heading = self.doc.add_heading(text, level=level)
        for run in heading.runs:
            run.font.size = Pt(16 if level == 2 else 14 if level == 3 else 12)

    def _add_paragraph(self, text: str):
        """添加段落（支持行内Markdown格式）"""
        from docx.shared import Pt
        para = self.doc.add_paragraph()
        self._add_inline_runs(para, text)

    def _add_bullet(self, text: str):
        """添加无序列表项"""
        from docx.shared import Pt
        text = re.sub(r"^[\-\*]\s+", "", text)
        para = self.doc.add_paragraph(style="List Bullet")
        self._add_inline_runs(para, text)

    def _add_numbered(self, text: str):
        """添加有序列表项"""
        text = re.sub(r"^\d+\.\s+", "", text)
        para = self.doc.add_paragraph(style="List Number")
        self._add_inline_runs(para, text)

    def _add_separator(self):
        """添加分隔线"""
        from docx.shared import Pt
        para = self.doc.add_paragraph()
        para.alignment = 1  # CENTER
        run = para.add_run("—")
        run.font.size = Pt(10)
        run.font.color.rgb = None  # 默认颜色

    def _add_inline_runs(self, paragraph, text: str):
        """
        解析行内Markdown格式，生成对应的Word Run

        支持：**粗体**、*斜体*、`代码`、[链接](url)
        """
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_UNDERLINE

        # 简单的正则解析
        pattern = r"(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|\[(.+?)\]\((.+?)\))"

        last_end = 0
        for match in re.finditer(pattern, text):
            # 添加匹配前的普通文本
            if match.start() > last_end:
                normal_text = text[last_end:match.start()]
                if normal_text:
                    paragraph.add_run(normal_text)

            full = match.group(0)
            if full.startswith("**"):
                run = paragraph.add_run(match.group(2))
                run.bold = True
            elif full.startswith("*"):
                run = paragraph.add_run(match.group(3))
                run.italic = True
            elif full.startswith("`"):
                run = paragraph.add_run(match.group(4))
                run.font.name = "Consolas"
                run.font.size = Pt(10)
            elif full.startswith("["):
                run = paragraph.add_run(match.group(5))
                run.font.color.rgb = RGBColor(0, 102, 204)
                run.underline = True

            last_end = match.end()

        # 添加剩余的普通文本
        if last_end < len(text):
            remaining = text[last_end:]
            if remaining:
                paragraph.add_run(remaining)
