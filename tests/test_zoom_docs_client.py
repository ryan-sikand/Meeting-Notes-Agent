from typing import Any

from app.main import process_zoom_doc_url
from app.zoom_docs_client import (
    ZoomDocsClient,
    ZoomDocsContentUnavailable,
    extract_runtime_value,
    is_zoom_docs_url,
)

ZOOM_DOC_SHELL = """
<html>
  <head>
    <meta property="og:title" content="Zoom Docs - open page" />
    <meta property="og:description" content="Someone shared a document with you." />
    <script>
      window.__RUNTIME_ENV__ = {
        "API_HOST":"https:\\/\\/docs.zoom.us",
        "FILE_ID":"abc123",
        "FILE_CLUSTER_API_PREFIX":"https:\\/\\/us01docs.zoom.us\\/"
      };
    </script>
  </head>
  <body><div id="root"></div></body>
</html>
"""


class FakeZoomDocsClient(ZoomDocsClient):
    def fetch_public_shell(self, url: str) -> str:
        assert url == "https://hub.zoom.us/doc/abc123?from=hub"
        return ZOOM_DOC_SHELL


def test_zoom_docs_inspection_detects_shell_without_content(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.ZoomDocsClient", FakeZoomDocsClient)

    result = process_zoom_doc_url("https://hub.zoom.us/doc/abc123?from=hub")

    assert result["processed"] is False
    assert result["reason"] == "content_unavailable"
    assert result["inspection"]["doc_id"] == "abc123"
    assert result["inspection"]["api_host"] == "https://docs.zoom.us"


def test_zoom_docs_client_helpers() -> None:
    assert is_zoom_docs_url("https://hub.zoom.us/doc/abc123?from=hub") is True
    assert is_zoom_docs_url("https://zoom.us/rec/share/abc123") is False
    assert extract_runtime_value(ZOOM_DOC_SHELL, "FILE_CLUSTER_API_PREFIX") == (
        "https://us01docs.zoom.us/"
    )

    try:
        FakeZoomDocsClient().download_text("https://hub.zoom.us/doc/abc123?from=hub")
    except ZoomDocsContentUnavailable as exc:
        assert "web app shell" in str(exc)
    else:
        raise AssertionError("Expected unavailable Zoom Docs content")
