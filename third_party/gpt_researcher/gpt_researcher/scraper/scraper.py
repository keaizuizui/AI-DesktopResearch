"""Web scraper module for GPT Researcher.

This module provides the Scraper class that extracts content from URLs
using various scraping backends (BeautifulSoup, pdfplumber, Browser, etc.).
"""

import asyncio
import importlib
import logging

import requests

from gpt_researcher.utils.workers import WorkerPool

from . import (
    ArxivScraper,
    BeautifulSoupScraper,
    BrowserScraper,
    FireCrawl,
    NoDriverScraper,
    PdfPlumberScraper,
    WebBaseLoaderScraper,
)


class Scraper:
    """
    Scraper class to extract the content from the links
    """

    def __init__(self, urls, user_agent, scraper, worker_pool: WorkerPool):
        """
        Initialize the Scraper class.
        Args:
            urls:
        """
        self.urls = urls
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.scraper = scraper
        if self.scraper == "firecrawl":
            self._check_pkg(self.scraper)
        self.logger = logging.getLogger(__name__)
        self.worker_pool = worker_pool

    async def run(self):
        """
        Extracts the content from the links
        """
        contents = await asyncio.gather(
            *(self.extract_data_from_url(url, self.session) for url in self.urls)
        )

        res = [content for content in contents if content["raw_content"] is not None]
        return res

    def _check_pkg(self, scrapper_name: str) -> None:
        """检查抓取器依赖是否安装；缺依赖直接报错，不自动安装。

        注意：上游版本会自动执行 pip install。本项目已移除该行为，
        避免运行时静默安装依赖带来的供应链风险。
        """
        pkg_map = {
            "firecrawl": {
                "package_installation_name": "firecrawl-py",
                "import_name": "firecrawl",
            },
        }
        pkg = pkg_map.get(scrapper_name)
        if not pkg:
            return
        if not importlib.util.find_spec(pkg["import_name"]):
            raise ImportError(
                f"{pkg['package_installation_name']} not found. "
                f"Please install manually with "
                f"`pip install -U {pkg['package_installation_name']}`"
            )

    async def extract_data_from_url(self, link, session):
        """
        Extracts the data from the link with logging
        """
        async with self.worker_pool.throttle():
            try:
                Scraper = self.get_scraper(link)
                scraper = Scraper(link, session)

                # Get scraper name
                scraper_name = scraper.__class__.__name__
                self.logger.info(f"\n=== Using {scraper_name} ===")

                # Get content
                if hasattr(scraper, "scrape_async"):
                    content, image_urls, title = await scraper.scrape_async()
                else:
                    (
                        content,
                        image_urls,
                        title,
                    ) = await asyncio.get_running_loop().run_in_executor(
                        self.worker_pool.executor, scraper.scrape
                    )

                if len(content) < 100:
                    self.logger.warning(f"Content too short or empty for {link}")
                    return {
                        "url": link,
                        "raw_content": None,
                        "image_urls": [],
                        "title": title,
                    }

                # Log results
                self.logger.info(f"\nTitle: {title}")
                self.logger.info(
                    f"Content length: {len(content) if content else 0} characters"
                )
                self.logger.info(f"Number of images: {len(image_urls)}")
                self.logger.info(f"URL: {link}")
                self.logger.info("=" * 50)

                if not content or len(content) < 100:
                    self.logger.warning(f"Content too short or empty for {link}")
                    return {
                        "url": link,
                        "raw_content": None,
                        "image_urls": [],
                        "title": title,
                    }

                return {
                    "url": link,
                    "raw_content": content,
                    "image_urls": image_urls,
                    "title": title,
                }

            except Exception as e:
                self.logger.error(f"Error processing {link}: {str(e)}")
                return {"url": link, "raw_content": None, "image_urls": [], "title": ""}

    def get_scraper(self, link):
        """
        The function `get_scraper` determines the appropriate scraper class based on the provided link
        or a default scraper if none matches.

        Args:
          link: The `get_scraper` method takes a `link` parameter which is a URL link to a webpage or a
        PDF file. Based on the type of content the link points to, the method determines the appropriate
        scraper class to use for extracting data from that content.

        Returns:
          The `get_scraper` method returns the scraper class based on the provided link. The method
        checks the link to determine the appropriate scraper class to use based on predefined mappings
        in the `SCRAPER_CLASSES` dictionary. If the link ends with ".pdf", it selects the
        `PdfPlumberScraper` class. If the link contains "arxiv.org", it selects the `ArxivScraper
        """

        SCRAPER_CLASSES = {
            "pdf": PdfPlumberScraper,
            "arxiv": ArxivScraper,
            "bs": BeautifulSoupScraper,
            "web_base_loader": WebBaseLoaderScraper,
            "browser": BrowserScraper,
            "nodriver": NoDriverScraper,
            "firecrawl": FireCrawl,
        }

        scraper_key = None

        if link.endswith(".pdf"):
            scraper_key = "pdf"
        elif "arxiv.org" in link:
            scraper_key = "arxiv"
        else:
            scraper_key = self.scraper

        scraper_class = SCRAPER_CLASSES.get(scraper_key)
        if scraper_class is None:
            raise Exception("Scraper not found.")

        return scraper_class
