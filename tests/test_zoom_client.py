from pathlib import Path
from typing import Any

from app.config import Settings
from app.main import zoom_auth_check, zoom_sync
from app.zoom_client import ZoomClient, ZoomRecording, ZoomRecordingFile


class FakeResponse:
    def __init__(
        self,
        json_body: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_body = json_body or {}
        self.content = content
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._json_body

    def raise_for_status(self) -> None:
        return None


class FakeHttpClient:
    post_calls: list[dict[str, Any]] = []
    get_calls: list[dict[str, Any]] = []
    protected_download_attempts = 0

    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(
            {
                "access_token": "zoom-token",
                "expires_in": 3600,
                "api_url": "https://api.zoom.us",
                "scope": "recording:read:admin",
            }
        )

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        if url.endswith("/users/me/recordings"):
            return FakeResponse(
                {
                    "meetings": [
                        {
                            "uuid": "meeting-uuid-1",
                            "id": 123,
                            "topic": "Acme Discovery",
                            "start_time": "2026-06-01T18:02:49Z",
                            "recording_files": [
                                {
                                    "id": "transcript-file-1",
                                    "file_type": "TRANSCRIPT",
                                    "file_extension": "VTT",
                                    "download_url": "https://download.example/transcript",
                                }
                            ],
                        }
                    ],
                    "next_page_token": "",
                }
            )
        if url.endswith("/meetings/meeting-uuid-1/recordings"):
            return FakeResponse(
                {
                    "uuid": "meeting-uuid-1",
                    "id": 123,
                    "topic": "Acme Discovery",
                    "start_time": "2026-06-01T18:02:49Z",
                    "recording_files": [
                        {
                            "id": "audio-file-1",
                            "file_type": "M4A",
                            "file_extension": "M4A",
                            "download_url": "https://download.example/audio",
                        }
                    ],
                }
            )
        if url == "https://download.example/protected":
            type(self).protected_download_attempts += 1
            return FakeResponse(status_code=401)
        return FakeResponse(content=b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nRyan: Hello")


def test_zoom_client_lists_recordings_and_downloads_transcript(monkeypatch: Any) -> None:
    FakeHttpClient.post_calls = []
    FakeHttpClient.get_calls = []
    FakeHttpClient.protected_download_attempts = 0
    monkeypatch.setattr("app.zoom_client.httpx.Client", FakeHttpClient)
    settings = Settings(
        ZOOM_ACCOUNT_ID="acct",
        ZOOM_CLIENT_ID="client",
        ZOOM_CLIENT_SECRET="secret",
    )

    client = ZoomClient(settings)
    recordings = client.list_recent_recordings(days_back=7)
    transcript = client.download_transcript("transcript-file-1")

    assert recordings[0].uuid == "meeting-uuid-1"
    assert recordings[0].has_transcript is True
    assert "Ryan: Hello" in transcript
    assert FakeHttpClient.post_calls[0]["data"]["grant_type"] == "account_credentials"
    assert FakeHttpClient.get_calls[0]["params"]["page_size"] == 100
    assert FakeHttpClient.get_calls[0]["url"].endswith("/users/me/recordings")


def test_zoom_client_gets_meeting_recording_and_download_fallback(monkeypatch: Any) -> None:
    FakeHttpClient.post_calls = []
    FakeHttpClient.get_calls = []
    FakeHttpClient.protected_download_attempts = 0
    monkeypatch.setattr("app.zoom_client.httpx.Client", FakeHttpClient)
    settings = Settings(
        ZOOM_ACCOUNT_ID="acct",
        ZOOM_CLIENT_ID="client",
        ZOOM_CLIENT_SECRET="secret",
    )

    client = ZoomClient(settings)
    recording = client.get_meeting_recording("meeting-uuid-1")
    client._download_urls["protected-file"] = "https://download.example/protected"
    content = client.download_recording_file("protected-file")

    assert recording.audio_file is not None
    assert recording.audio_file.id == "audio-file-1"
    assert b"Ryan: Hello" in content
    assert FakeHttpClient.protected_download_attempts == 1
    assert any("access_token=zoom-token" in call["url"] for call in FakeHttpClient.get_calls)


class FakeZoomClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_recent_recordings(self, days_back: int = 7) -> list[ZoomRecording]:
        assert days_back == 3
        return [
            ZoomRecording(
                uuid="new-with-transcript",
                id=1,
                topic="Acme Discovery",
                start_time="2026-06-01T18:02:49Z",
                recording_files=[
                    ZoomRecordingFile(
                        id="transcript-1",
                        file_type="TRANSCRIPT",
                        download_url="https://download.example/transcript",
                    )
                ],
            ),
            ZoomRecording(
                uuid="missing-transcript",
                id=2,
                topic="No Transcript",
                start_time="2026-06-01T19:00:00Z",
                recording_files=[],
            ),
        ]

    def download_transcript(self, recording_id: str) -> str:
        assert recording_id == "transcript-1"
        return "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nRyan: Hello"

    def write_transcript_file(self, recording: ZoomRecording, transcript_text: str) -> Path:
        self.settings.zoom_download_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.zoom_download_dir / f"{recording.uuid}.vtt"
        path.write_text(transcript_text, encoding="utf-8")
        return path

    def transcribe_audio(self, recording: ZoomRecording) -> str | None:
        raise AssertionError(f"Should not transcribe when disabled: {recording.uuid}")


def test_zoom_sync_processes_transcripts_and_skips_missing(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("app.main.ZoomClient", FakeZoomClient)
    settings = Settings(
        DATA_DIR=tmp_path / "runs",
        ZOOM_DOWNLOAD_DIR=tmp_path / "zoom",
        DRY_RUN=True,
        TRANSCRIBE_AUDIO=False,
        OPENAI_API_KEY=None,
        SALESFORCE_CLI_ENABLED=False,
    )

    result = zoom_sync(days_back=3, settings=settings)

    assert len(result["processed_run_ids"]) == 1
    assert result["skipped"] == [{"uuid": "missing-transcript", "reason": "missing_transcript"}]

    second_result = zoom_sync(days_back=3, settings=settings)

    assert second_result["processed_run_ids"] == []
    assert {"uuid": "new-with-transcript", "reason": "already_processed"} in second_result[
        "skipped"
    ]


def test_zoom_auth_check_reports_missing_credentials(tmp_path: Path) -> None:
    settings = Settings(DATA_DIR=tmp_path / "runs")

    result = zoom_auth_check(settings=settings)

    assert result == {"configured": False, "message": "Zoom credentials are not configured."}
