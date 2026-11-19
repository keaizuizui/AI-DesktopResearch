#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sanitize.py - 清理 docx 文件中的敏感信息

功能：
1. 扫描 docx 文件中的潜在敏感信息（API Key、密码等）
2. 替换或删除敏感内容
3. 生成清理后的文件

使用方法：
    python sanitize.py <input.docx> [output.docx]

如果不指定输出文件，则覆盖原文件（原文件备份为 .bak）
"""

import sys
import os
import re
import shutil
import zipfile
import tempfile
from pathlib import Path


# 敏感信息匹配模式
SENSITIVE_PATTERNS = [
    # API Keys
    (r'sk-[a-zA-Z0-9]{48}', '[API_KEY_REDACTED]'),  # OpenAI API Key
    (r'[a-zA-Z0-9]{32}-[a-zA-Z0-9]{32}', '[API_KEY_REDACTED]'),  # 通用 API Key 格式
    (r'[a-f0-9]{64}', '[API_KEY_REDACTED]'),  # 十六进制 API Key
    # 密码
    (r'password\s*=\s*["\'][^"\']+["\']', 'password = [REDACTED]'),
    (r'passwd\s*=\s*["\'][^"\']+["\']', 'passwd = [REDACTED]'),
    (r'pwd\s*=\s*["\'][^"\']+["\']', 'pwd = [REDACTED]'),
    # 密钥
    (r'secret\s*=\s*["\'][^"\']+["\']', 'secret = [REDACTED]'),
    (r'key\s*=\s*["\'][^"\']{16,}["\']', 'key = [REDACTED]'),
    # 邮箱密码
    (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[^\s@]+', '[EMAIL_CREDENTIALS_REDACTED]'),
    # IP 地址（可选，根据需要启用）
    # (r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b', '[IP_REDACTED]'),
]


def sanitize_text(text: str) -> str:
    """清理文本中的敏感信息"""
    cleaned = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return cleaned


def sanitize_docx(input_path: str, output_path: str = None) -> str:
    """
    清理 docx 文件中的敏感信息

    Args:
        input_path: 输入 docx 文件路径
        output_path: 输出 docx 文件路径（可选）

    Returns:
        输出文件路径
    """
    input_path = Path(input_path)

    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在: {input_path}")

    if not input_path.suffix.lower() == '.docx':
        raise ValueError(f"只支持 .docx 文件: {input_path}")

    # 确定输出路径
    if output_path is None:
        # 备份原文件
        backup_path = input_path.with_suffix('.docx.bak')
        shutil.copy2(input_path, backup_path)
        print(f"已备份原文件: {backup_path}")
        output_path = input_path
    else:
        output_path = Path(output_path)

    # 创建临时目录
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # 解压 docx 文件
        with zipfile.ZipFile(input_path, 'r') as zip_ref:
            zip_ref.extractall(temp_path)

        # 处理 document.xml（主要内容）
        doc_xml = temp_path / 'word' / 'document.xml'
        if doc_xml.exists():
            with open(doc_xml, 'r', encoding='utf-8') as f:
                content = f.read()

            cleaned_content = sanitize_text(content)

            if content != cleaned_content:
                with open(doc_xml, 'w', encoding='utf-8') as f:
                    f.write(cleaned_content)
                print("已清理 document.xml 中的敏感信息")

        # 处理 comments.xml（批注）
        comments_xml = temp_path / 'word' / 'comments.xml'
        if comments_xml.exists():
            with open(comments_xml, 'r', encoding='utf-8') as f:
                content = f.read()

            cleaned_content = sanitize_text(content)

            if content != cleaned_content:
                with open(comments_xml, 'w', encoding='utf-8') as f:
                    f.write(cleaned_content)
                print("已清理 comments.xml 中的敏感信息")

        # 处理 core.xml（文档属性）
        core_xml = temp_path / 'docProps' / 'core.xml'
        if core_xml.exists():
            with open(core_xml, 'r', encoding='utf-8') as f:
                content = f.read()

            # 清理作者信息中的敏感内容
            cleaned_content = sanitize_text(content)

            if content != cleaned_content:
                with open(core_xml, 'w', encoding='utf-8') as f:
                    f.write(cleaned_content)
                print("已清理 core.xml 中的敏感信息")

        # 重新打包 docx 文件
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in temp_path.rglob('*'):
                if file_path.is_file():
                    arcname = str(file_path.relative_to(temp_path))
                    zipf.write(file_path, arcname)

    print(f"清理完成: {output_path}")
    return str(output_path)


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"\n用法: python {sys.argv[0]} <input.docx> [output.docx]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        result = sanitize_docx(input_file, output_file)
        print(f"\n成功生成清理后的文件: {result}")
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
