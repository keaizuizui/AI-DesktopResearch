"""
shared/document/excel — Excel导出
==================================

统一Excel导出接口，消除方案工具和调研工具中的样式代码重复。

功能：
- 统一样式定义（字体/填充/对齐/边框）
- 多套预设主题（方案工具深蓝/调研工具蓝色/通用）
- 条件行着色（阶段颜色/信源等级/置信度）
- 列宽自适应
- 通用Excel导出器

迁移自：
- 方案工具 schedule_export.py 的样式常量和方法
- 调研工具 deliverables.py 的Excel样式代码
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 预设颜色方案
# ═══════════════════════════════════════════════════════════════

# 方案工具：前宣/中宣/后宣阶段颜色
PHASE_COLORS_SCHEME = {
    "前宣": {"fill": "DAEEF3", "font": "1F4E79"},
    "中宣": {"fill": "E2EFDA", "font": "375623"},
    "后宣": {"fill": "FCE4D6", "font": "843C0C"},
}

# 调研工具：信源等级颜色
TIER_COLORS = {
    "S": "C6EFCE",   # 绿色 — 权威
    "A": "FFEB9C",   # 黄色 — 可信
    "B": "FFC7CE",   # 浅红 — 一般
    "C": "FF6B6B",   # 红色 — 低质
}

# 调研工具：置信度颜色
CONFIDENCE_COLORS = {
    "high": "C6EFCE",
    "medium": "FFEB9C",
    "low": "FFC7CE",
}

# 通用阶段颜色（英文key，供程序使用）
DEFAULT_PHASE_COLORS = {
    "pre": "E8F4FD",
    "during": "FFF3E0",
    "post": "E8F5E9",
    "default": "F5F5F5",
}


# ═══════════════════════════════════════════════════════════════
# 样式工具类
# ═══════════════════════════════════════════════════════════════

class ExcelStyles:
    """
    统一样式工具

    提供三套预设主题 + 自定义颜色支持：
    - "scheme": 方案工具深蓝主题（header_fill=1F4E79）
    - "research": 调研工具蓝色主题（header_fill=2E75B6）
    - "default": 通用蓝色主题（header_fill=4472C4）
    """

    # 主题预设
    THEMES = {
        "scheme": {
            "header_fill": "1F4E79",
            "header_font_color": "FFFFFF",
            "data_font_name": "微软雅黑",
            "data_font_size": 10,
        },
        "research": {
            "header_fill": "2E75B6",
            "header_font_color": "FFFFFF",
            "data_font_name": "Microsoft YaHei",
            "data_font_size": 10,
        },
        "default": {
            "header_fill": "4472C4",
            "header_font_color": "FFFFFF",
            "data_font_name": "Microsoft YaHei",
            "data_font_size": 10,
        },
    }

    def __init__(self, theme: str = "default"):
        """
        Args:
            theme: 主题名称 "scheme" / "research" / "default"
        """
        self._theme = self.THEMES.get(theme, self.THEMES["default"])

    # ── 表头样式 ──

    def apply_header_style(self, cell, font_name: str = "", font_size: int = 11):
        """应用表头样式（使用当前主题颜色）"""
        from openpyxl.styles import Font, PatternFill, Alignment

        name = font_name or self._theme["data_font_name"]
        cell.font = Font(name=name, size=font_size, bold=True,
                         color=self._theme["header_font_color"])
        cell.fill = PatternFill(
            start_color=self._theme["header_fill"],
            end_color=self._theme["header_fill"],
            fill_type="solid",
        )
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = self.default_border()

    @staticmethod
    def apply_header_style_custom(cell, fill_color: str = "4472C4",
                                   font_color: str = "FFFFFF",
                                   font_name: str = "Microsoft YaHei",
                                   font_size: int = 11):
        """应用自定义表头样式"""
        from openpyxl.styles import Font, PatternFill, Alignment

        cell.font = Font(name=font_name, size=font_size, bold=True, color=font_color)
        cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = ExcelStyles.default_border()

    # ── 数据行样式 ──

    def apply_data_style(self, cell, font_size: int = 0):
        """应用默认数据行样式"""
        from openpyxl.styles import Font, Alignment

        cell.font = Font(
            name=self._theme["data_font_name"],
            size=font_size or self._theme["data_font_size"],
        )
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = self.default_border()

    # ── 条件着色 ──

    @staticmethod
    def apply_conditional_fill(cell, fill_color: str, font_color: str = "000000"):
        """给单元格应用条件颜色填充"""
        from openpyxl.styles import Font, PatternFill

        cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        cell.font = Font(
            name=cell.font.name if cell.font.name else "Microsoft YaHei",
            size=cell.font.size if cell.font.size else 10,
            color=font_color,
        )

    @staticmethod
    def apply_tier_color(cell, tier: str):
        """按信源等级着色（S/A/B/C）"""
        color = TIER_COLORS.get(tier, TIER_COLORS["B"])
        ExcelStyles.apply_conditional_fill(cell, fill_color=color)

    @staticmethod
    def apply_confidence_color(cell, confidence: str):
        """按置信度着色（high/medium/low）"""
        color = CONFIDENCE_COLORS.get(confidence, CONFIDENCE_COLORS["medium"])
        ExcelStyles.apply_conditional_fill(cell, fill_color=color)

    @staticmethod
    def apply_phase_color(cell, phase: str, color_map: Optional[Dict] = None):
        """
        按阶段着色

        Args:
            cell: openpyxl单元格
            phase: 阶段名（如"前宣"/"中宣"/"后宣"）
            color_map: 颜色映射字典，默认使用PHASE_COLORS_SCHEME
        """
        colors = color_map or PHASE_COLORS_SCHEME
        if phase in colors:
            cfg = colors[phase]
            ExcelStyles.apply_conditional_fill(cell, cfg["fill"], cfg.get("font", "000000"))

    # ── 边框 ──

    @staticmethod
    def default_border():
        """默认细边框"""
        from openpyxl.styles import Border, Side
        thin = Side(style="thin", color="D9D9D9")
        return Border(left=thin, right=thin, top=thin, bottom=thin)

    # ── 列宽 ──

    @staticmethod
    def auto_width(ws, min_width: int = 10, max_width: int = 50):
        """自动调整列宽（中文字符算2个宽度）"""
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    length = sum(2 if ord(c) > 127 else 1 for c in str(cell.value))
                    max_length = max(max_length, length)
            adjusted = min(max(max_length + 2, min_width), max_width)
            ws.column_dimensions[col_letter].width = adjusted

    @staticmethod
    def set_column_widths(ws, width_dict: Dict[str, int]):
        """按列字母设置固定列宽"""
        for col_letter, width in width_dict.items():
            ws.column_dimensions[col_letter].width = width

    # ── 冻结 ──

    @staticmethod
    def freeze_header(ws, header_row: int = 1):
        """冻结表头行"""
        ws.freeze_panes = f"A{header_row + 1}"

    # ── 兼容旧接口 ──

    # 保留兼容静态方法签名，避免破坏已有调用
    DEFAULT_PHASE_COLORS = DEFAULT_PHASE_COLORS

    @staticmethod
    def apply_cell_style(cell, phase: str = "default"):
        """兼容接口：按阶段key着色"""
        if cell is None:
            return
        colors = DEFAULT_PHASE_COLORS
        fill_color = colors.get(phase, colors["default"])
        from openpyxl.styles import Font, PatternFill, Alignment
        cell.font = Font(name="Microsoft YaHei", size=10)
        cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = ExcelStyles.default_border()

    @staticmethod
    def _default_border():
        """兼容接口"""
        return ExcelStyles.default_border()


# ═══════════════════════════════════════════════════════════════
# 通用Excel导出器
# ═══════════════════════════════════════════════════════════════

class ExcelExporter:
    """
    通用Excel导出器

    使用方式：
        exporter = ExcelExporter(theme="scheme")
        exporter.add_sheet("排期表", headers, rows, phase_column=2)
        exporter.add_sheet("附录", headers2, rows2)
        data = exporter.to_bytesio()
    """

    def __init__(self, theme: str = "default"):
        self._sheets: List[Dict[str, Any]] = []
        self._styles = ExcelStyles(theme)

    def add_sheet(
        self,
        sheet_name: str,
        headers: List[str],
        rows: List[List[Any]],
        phase_column: int = -1,
        title: str = "",
        color_map: Optional[Dict] = None,
    ):
        """
        添加工作表

        Args:
            sheet_name: 工作表名称
            headers: 表头列表
            rows: 数据行列表
            phase_column: 阶段列索引（用于颜色区分），-1表示不区分
            title: 表格标题（可选）
            color_map: 自定义阶段颜色映射（可选）
        """
        self._sheets.append({
            "name": sheet_name,
            "headers": headers,
            "rows": rows,
            "phase_column": phase_column,
            "title": title,
            "color_map": color_map,
        })

    def to_bytesio(self) -> io.BytesIO:
        """导出为BytesIO字节流"""
        from openpyxl import Workbook

        wb = Workbook()
        for i, sheet_cfg in enumerate(self._sheets):
            if i == 0:
                ws = wb.active
            else:
                ws = wb.create_sheet(title=sheet_cfg["name"])
            ws.title = sheet_cfg["name"]

            headers = sheet_cfg["headers"]
            rows = sheet_cfg["rows"]
            phase_col = sheet_cfg["phase_column"]
            color_map = sheet_cfg.get("color_map")

            # 标题行
            start_row = 1
            if sheet_cfg["title"]:
                ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
                title_cell = ws.cell(row=1, column=1, value=sheet_cfg["title"])
                from openpyxl.styles import Font, Alignment
                title_cell.font = Font(name="Microsoft YaHei", size=14, bold=True)
                title_cell.alignment = Alignment(horizontal="center")
                start_row = 2

            # 表头
            for j, header in enumerate(headers, 1):
                cell = ws.cell(row=start_row, column=j, value=header)
                self._styles.apply_header_style(cell)

            # 数据行
            for r_idx, row_data in enumerate(rows, start_row + 1):
                for c_idx, value in enumerate(row_data, 1):
                    cell = ws.cell(row=r_idx, column=c_idx, value=value)
                    self._styles.apply_data_style(cell)
                    # 阶段颜色
                    if phase_col >= 0 and c_idx == phase_col + 1 and color_map:
                        self._styles.apply_phase_color(cell, str(value), color_map)

            # 自动列宽 + 冻结
            ExcelStyles.auto_width(ws)
            ExcelStyles.freeze_header(ws, start_row)

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return buffer

    def save(self, filepath: str):
        """保存到文件"""
        buffer = self.to_bytesio()
        with open(filepath, "wb") as f:
            f.write(buffer.getvalue())
        logger.info(f"[ExcelExporter] 已保存: {filepath}")
