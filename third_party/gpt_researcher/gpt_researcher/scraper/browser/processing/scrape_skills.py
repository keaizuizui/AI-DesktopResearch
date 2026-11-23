from langchain_community.retrievers import ArxivRetriever
# [PATCHED] 使用 pdfplumber (MIT) 替代 PyMuPDFLoader (AGPL)
from gpt_researcher.patches.pdfplumber_loader import load_pdf_with_pdfplumber


def scrape_pdf_with_pdfplumber(url) -> str:
    """[PATCHED] 使用 pdfplumber (MIT) 替代 PyMuPDF"""
    return load_pdf_with_pdfplumber(url)


def scrape_pdf_with_arxiv(query) -> str:
    """Scrape a pdf with arxiv
    default document length of 70000 about ~15 pages or None for no limit

    Args:
        query (str): The query to search for

    Returns:
        str: The text scraped from the pdf
    """
    retriever = ArxivRetriever(load_max_docs=2, doc_content_chars_max=None)
    docs = retriever.get_relevant_documents(query=query)
    return docs[0].page_content