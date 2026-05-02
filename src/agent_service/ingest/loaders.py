import io
from pathlib import Path

from bs4 import BeautifulSoup


SUPPORTED_MIME = {
    "text/plain",
    "text/markdown",
    "text/html",
    "application/pdf",
    "application/octet-stream",  # tolerated, dispatch by extension
}


def detect_mime(filename: str, declared: str | None) -> str:
    if declared and declared != "application/octet-stream":
        return declared
    ext = Path(filename).suffix.lower()
    return {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
    }.get(ext, "text/plain")


def extract_text(data: bytes, mime: str) -> str:
    if mime == "application/pdf":
        return _extract_pdf(data)
    if mime == "text/html":
        return _extract_html(data)
    # markdown / plain — return as-is
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="ignore")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader  # lazy: avoids loading cryptography unless needed
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_html(data: bytes) -> str:
    soup = BeautifulSoup(data, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    return soup.get_text(separator="\n")
