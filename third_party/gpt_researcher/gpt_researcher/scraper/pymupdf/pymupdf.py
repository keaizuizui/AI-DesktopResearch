import os
import requests
import tempfile
import pdfplumber
from urllib.parse import urlparse


class PyMuPDFScraper:
    """[PATCHED] 使用 pdfplumber (MIT) 替代 PyMuPDFLoader (AGPL)"""

    def __init__(self, link, session=None):
        self.link = link
        self.session = session

    def is_url(self) -> bool:
        try:
            result = urlparse(self.link)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    def scrape(self) -> tuple[str, list[str], str]:
        """[PATCHED] 使用 pdfplumber (MIT) 替代 PyMuPDFLoader"""
        try:
            if self.is_url():
                response = requests.get(self.link, timeout=30, stream=True)
                response.raise_for_status()

                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                    temp_filename = temp_file.name
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                try:
                    with pdfplumber.open(temp_filename) as pdf:
                        texts = []
                        for page in pdf.pages:
                            t = page.extract_text()
                            if t:
                                texts.append(t)
                        return "\n\n".join(texts), [], ""
                finally:
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