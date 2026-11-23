import os
import time
import requests
import tempfile
import pdfplumber
from urllib.parse import urlparse


class PdfPlumberScraper:
    """PDF 抓取器，使用 pdfplumber (MIT) 解析，避免 AGPL 依赖。"""

    def __init__(self, link, session=None):
        self.link = link
        self.session = session

    def is_url(self) -> bool:
        try:
            result = urlparse(self.link)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    MAX_BYTES = 50 * 1024 * 1024  # 单个 PDF 下载上限，避免磁盘被占满
    MAX_SECONDS = 60              # 下载总耗时上限，避免慢速连接长期占用

    def scrape(self) -> tuple[str, list[str], str]:
        try:
            if self.is_url():
                temp_filename = None
                try:
                    client = self.session or requests
                    response = client.get(self.link, timeout=30, stream=True)
                    response.raise_for_status()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                        temp_filename = temp_file.name
                        total = 0
                        started = time.time()
                        for chunk in response.iter_content(chunk_size=8192):
                            total += len(chunk)
                            if total > self.MAX_BYTES:
                                raise ValueError("PDF 超过下载上限")
                            if time.time() - started > self.MAX_SECONDS:
                                raise ValueError("PDF 下载超时")
                            temp_file.write(chunk)
                    with pdfplumber.open(temp_filename) as pdf:
                        texts = []
                        for page in pdf.pages:
                            t = page.extract_text()
                            if t:
                                texts.append(t)
                        return "\n\n".join(texts), [], ""
                finally:
                    if temp_filename and os.path.exists(temp_filename):
                        os.remove(temp_filename)
            else:
                with pdfplumber.open(self.link) as pdf:
                    texts = []
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            texts.append(t)
                    return "\n\n".join(texts), [], ""

        except requests.exceptions.Timeout:
            print(f"Download timed out. Please check the link : {self.link}")
            return "", [], ""
        except Exception as e:
            print(f"Error loading PDF : {self.link} {e}")
            return "", [], ""
