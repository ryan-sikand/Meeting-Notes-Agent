import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

ZOOM_DOC_URL_RE = re.compile(r"https://hub\.zoom\.us/doc/(?P<doc_id>[^/?#]+)")
RUNTIME_ENV_RE = re.compile(r"window\.__RUNTIME_ENV__\s*=\s*(?P<json>\{.*?\});", re.DOTALL)
META_RE = re.compile(
    r'<meta\s+(?:property|name)="(?P<name>[^"]+)"\s+content="(?P<content>[^"]*)"',
    re.IGNORECASE,
)


class ZoomDocsContentUnavailable(RuntimeError):
    pass


def is_zoom_docs_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc == "hub.zoom.us"
        and "/doc/" in parsed.path
    )


def extract_zoom_doc_id(url: str) -> str:
    match = ZOOM_DOC_URL_RE.search(url)
    if not match:
        raise ValueError(f"Not a supported Zoom Docs URL: {url}")
    return match.group("doc_id")


class ZoomDocsClient:
    def fetch_public_shell(self, url: str) -> str:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        return response.text

    def inspect(self, url: str) -> dict[str, Any]:
        doc_id = extract_zoom_doc_id(url)
        html = self.fetch_public_shell(url)
        metadata = extract_meta(html)
        api_host = extract_runtime_value(html, "API_HOST")
        cluster_api_prefix = extract_runtime_value(html, "FILE_CLUSTER_API_PREFIX")
        content_text = extract_visible_doc_text(html)
        return {
            "doc_id": doc_id,
            "title": metadata.get("og:title"),
            "description": metadata.get("og:description"),
            "api_host": api_host,
            "file_cluster_api_prefix": cluster_api_prefix,
            "has_embedded_content": bool(content_text),
            "content_text": content_text,
        }

    def download_text(self, url: str) -> str:
        inspection = self.inspect(url)
        content = inspection["content_text"]
        if content:
            return str(content)
        raise ZoomDocsContentUnavailable(
            "Zoom Docs link returned only the web app shell. The document content requires "
            "authenticated Zoom Docs APIs or browser-session export access."
        )


def extract_meta(html: str) -> dict[str, str]:
    return {
        match.group("name"): unescape(match.group("content"))
        for match in META_RE.finditer(html)
    }


def extract_runtime_value(html: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}":"(?P<value>[^"]+)"', html)
    if not match:
        return None
    return match.group("value").replace("\\/", "/")


def extract_visible_doc_text(html: str) -> str | None:
    root_match = re.search(r'<div id="root">(?P<content>.*?)</div>', html, re.DOTALL)
    if not root_match:
        return None
    content = re.sub(r"<[^>]+>", " ", root_match.group("content"))
    normalized = " ".join(unescape(content).split())
    return normalized or None
