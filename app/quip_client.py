from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import safe_filename


class QuipClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def create_document(
        self,
        title: str,
        html_content: str,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.dry_run or not self.settings.quip_access_token:
            return self.write_dry_run_file(title, html_content)

        payload = {
            "title": title,
            "content": html_content,
            "format": "html",
            "member_ids": [folder_id or self.settings.quip_folder_id],
        }
        return self.post("/1/threads/new-document", payload)

    def append_to_document(self, thread_id: str, html_content: str) -> dict[str, Any]:
        if self.settings.dry_run or not self.settings.quip_access_token:
            return self.write_dry_run_file(f"append-{thread_id}", html_content)
        return self.post(
            "/1/threads/edit-document",
            {
                "thread_id": thread_id,
                "content": html_content,
                "format": "html",
                "operation": "APPEND",
            },
        )

    def write_dry_run_file(self, title: str, html_content: str) -> dict[str, Any]:
        self.settings.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.out_dir / f"{safe_filename(title)}.html"
        path.write_text(html_content, encoding="utf-8")
        return {"dry_run": True, "path": str(path)}

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.quip_access_token}"}
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.quip_base_url.rstrip('/')}{path}",
                headers=headers,
                data={key: value for key, value in payload.items() if value is not None},
            )
            response.raise_for_status()
        return response.json()


def local_html_path(title: str, out_dir: Path) -> Path:
    return out_dir / f"{safe_filename(title)}.html"
