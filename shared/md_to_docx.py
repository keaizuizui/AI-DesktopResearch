"""
Markdown 转 Word 适配器

优先使用 pandoc（效果最好），降级到 python-docx（无需额外依赖）

使用：
    from shared.md_to_docx import markdown_to_docx
    markdown_to_docx("# 标题\n\n内容", "output.docx")
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _check_pandoc() -> bool:
    """检查系统是否安装了 pandoc"""
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _convert_with_pandoc(markdown_text: str, output_path: str) -> bool:
    """使用 pandoc 转换（效果最好）"""
    try:
        # 创建临时 markdown 文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write(markdown_text)
            md_path = f.name
        
        # 调用 pandoc
        try:
            result = subprocess.run(
                ["pandoc", md_path, "-o", output_path, "-f", "markdown", "-t", "docx"],
                capture_output=True,
                text=True,
                timeout=60
            )
        except FileNotFoundError:
            os.unlink(md_path)
            logger.error("[pandoc] 未安装，请执行: pip install pypandoc 或安装 pandoc")
            return False
        except subprocess.TimeoutExpired:
            os.unlink(md_path)
            logger.error("[pandoc] 转换超时")
            return False
        except Exception as e:
            os.unlink(md_path)
            logger.error(f"[pandoc] 转换失败: {e}")
            return False
        
        # 清理临时文件
        os.unlink(md_path)
        
        if result.returncode == 0:
            logger.info(f"[pandoc] 转换成功: {output_path}")
            return True
        else:
            logger.warning(f"[pandoc] 转换失败: {result.stderr}")
            return False
            
    except Exception as e:
        logger.warning(f"[pandoc] 异常: {e}")
        return False


def _convert_with_python_docx(markdown_text: str, output_path: str) -> bool:
    """使用 python-docx 转换（降级方案）"""
    try:
        from docx import Document
        from docx.shared import Pt, Inches, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        
        doc = Document()
        
        # 简单的 Markdown 解析
        lines = markdown_text.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            # 标题
            if stripped.startswith('# '):
                p = doc.add_heading(stripped[2:], level=1)
            elif stripped.startswith('## '):
                p = doc.add_heading(stripped[3:], level=2)
            elif stripped.startswith('### '):
                p = doc.add_heading(stripped[4:], level=3)
            elif stripped.startswith('#### '):
                p = doc.add_heading(stripped[5:], level=4)
            # 列表
            elif stripped.startswith('- ') or stripped.startswith('* '):
                doc.add_paragraph(stripped[2:], style='List Bullet')
            elif stripped.startswith('1. ') or stripped.startswith('2. ') or stripped.startswith('3. '):
                doc.add_paragraph(stripped[3:], style='List Number')
            # 空行
            elif not stripped:
                pass
            # 普通段落
            else:
                doc.add_paragraph(stripped)
            
            i += 1
        
        doc.save(output_path)
        logger.info(f"[python-docx] 转换成功: {output_path}")
        return True
        
    except Exception as e:
        logger.error(f"[python-docx] 转换失败: {e}")
        return False


def markdown_to_docx(markdown_text: str, output_path: str, use_pandoc: bool = True) -> bool:
    """
    Markdown 转 Word
    
    Args:
        markdown_text: Markdown 文本
        output_path: 输出 docx 路径
        use_pandoc: 是否尝试使用 pandoc（默认True）
    
    Returns:
        是否成功
    """
    # 确保输出目录存在
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    
    # 优先尝试 pandoc
    if use_pandoc and _check_pandoc():
        if _convert_with_pandoc(markdown_text, output_path):
            return True
        logger.info("[md_to_docx] pandoc 失败，降级到 python-docx")
    
    # 降级到 python-docx
    return _convert_with_python_docx(markdown_text, output_path)


def is_pandoc_available() -> bool:
    """检查 pandoc 是否可用"""
    return _check_pandoc()
