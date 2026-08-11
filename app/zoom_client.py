import base64
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings, get_settings
from app.models import safe_filename
from app.openai_client import transcribe_audio_file

LOGGER = logging.getLogger(__name__)
ZOOM_AUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE_URL = "https://api.zoom.us/v2"
TRANSCRIPT_TYPES = {"TRANSCRIPT", "VTT", "CC"}
AUDIO_TYPES = {"M4A", "MP3", "WAV"}
TOKEN_EXPIRY_SKEW_SECONDS = 60


@dataclass(frozen=True)
class ZoomAccessToken:
    access_token: str
    api_url: str
    expires_at: datetime
    scopes: str | None = None

    @property
    def is_valid(self) -> bool:
        return datetime.now(UTC) < self.expires_at


@dataclass(frozen=True)
class ZoomRecordingFile:
    id: str
    file_type: str
    download_url: str
    file_extension: str | None = None
    recording_type: str | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "ZoomRecordingFile":
        return cls(
            id=str(payload["id"]),
            file_type=str(payload.get("file_type", "")).upper(),
            download_url=str(payload.get("download_url", "")),
            file_extension=payload.get("file_extension"),
            recording_type=payload.get("recording_type"),
        )


@dataclass(frozen=True)
class ZoomRecording:
    uuid: str
    id: int | str | None
    topic: str
    start_time: str | None
    recording_files: list[ZoomRecordingFile]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "ZoomRecording":
        return cls(
            uuid=str(payload["uuid"]),
            id=payload.get("id"),
            topic=str(payload.get("topic") or "Zoom Meeting"),
            start_time=payload.get("start_time"),
            recording_files=[
                ZoomRecordingFile.from_api(file_payload)
                for file_payload in payload.get("recording_files", [])
                if file_payload.get("id")
            ],
        )

    @property
    def transcript_file(self) -> ZoomRecordingFile | None:
        return next(
            (file for file in self.recording_files if file.file_type in TRANSCRIPT_TYPES),
            None,
        )

    @property
    def audio_file(self) -> ZoomRecordingFile | None:
        return next(
            (file for file in self.recording_files if file.file_type in AUDIO_TYPES),
            None,
        )

    @property
    def has_transcript(self) -> bool:
        return self.transcript_file is not None


class ZoomClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.token: ZoomAccessToken | None = None
        self._download_urls: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return all(
            [
                self.settings.zoom_account_id,
                self.settings.zoom_client_id,
                self.settings.zoom_client_secret,
            ]
        )

    def authenticate(self) -> str:
        if self.token and self.token.is_valid:
            return self.token.access_token
        if not self.configured:
            raise RuntimeError("Zoom Server-to-Server OAuth credentials are not configured.")

        credentials = f"{self.settings.zoom_client_id}:{self.settings.zoom_client_secret}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        headers = {"Authorization": f"Basic {encoded}"}
        data = {
            "grant_type": "account_credentials",
            "account_id": self.settings.zoom_account_id,
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(ZOOM_AUTH_URL, headers=headers, data=data)
            response.raise_for_status()
        body = response.json()
        expires_in = int(body.get("expires_in", 3600)) - TOKEN_EXPIRY_SKEW_SECONDS
        self.token = ZoomAccessToken(
            access_token=body["access_token"],
            api_url=str(body.get("api_url") or ZOOM_API_BASE_URL).rstrip("/"),
            expires_at=datetime.now(UTC) + timedelta(seconds=max(expires_in, 60)),
            scopes=body.get("scope"),
        )
        return self.token.access_token

    @property
    def api_base_url(self) -> str:
        if self.token and self.token.api_url:
            if self.token.api_url.endswith("/v2"):
                return self.token.api_url
            return f"{self.token.api_url}/v2"
        return ZOOM_API_BASE_URL

    def list_recent_recordings(self, days_back: int = 7) -> list[ZoomRecording]:
        if not self.configured:
            LOGGER.warning("Zoom credentials are not configured; returning no recordings.")
            return []

        today = date.today()
        params: dict[str, Any] = {
            "from": (today - timedelta(days=days_back)).isoformat(),
            "to": today.isoformat(),
            "page_size": 100,
        }
        recordings: list[ZoomRecording] = []
        while True:
            body = self.get_json(
                f"/users/{quote(self.settings.zoom_user_id, safe='')}/recordings",
                params=params,
            )
            for meeting_payload in body.get("meetings", []) or []:
                recording = self.hydrate_recording_files(ZoomRecording.from_api(meeting_payload))
                recordings.append(recording)
            next_page_token = body.get("next_page_token")
            if not next_page_token:
                break
            params["next_page_token"] = next_page_token
        return recordings

    def get_meeting_recording(self, meeting_uuid: str) -> ZoomRecording:
        encoded_uuid = quote(meeting_uuid, safe="")
        body = self.get_json(f"/meetings/{encoded_uuid}/recordings")
        return self.hydrate_recording_files(ZoomRecording.from_api(body))

    def hydrate_recording_files(self, recording: ZoomRecording) -> ZoomRecording:
        for file in recording.recording_files:
            if file.download_url:
                self._download_urls[file.id] = file.download_url
        return recording

    def download_transcript(self, recording_id: str) -> str:
        content = self.download_recording_file(recording_id)
        return content.decode("utf-8-sig")

    def download_audio(self, recording_id: str) -> bytes:
        return self.download_recording_file(recording_id)

    def write_transcript_file(self, recording: ZoomRecording, transcript_text: str) -> Path:
        self.settings.zoom_download_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".vtt" if transcript_text.lstrip().upper().startswith("WEBVTT") else ".txt"
        path = self.settings.zoom_download_dir / f"{zoom_file_stem(recording)}{suffix}"
        path.write_text(transcript_text, encoding="utf-8")
        return path

    def write_audio_file(self, recording: ZoomRecording, audio: bytes) -> Path:
        self.settings.zoom_download_dir.mkdir(parents=True, exist_ok=True)
        extension = recording.audio_file.file_extension if recording.audio_file else "m4a"
        path = self.settings.zoom_download_dir / f"{zoom_file_stem(recording)}.{extension.lower()}"
        path.write_bytes(audio)
        return path

    def transcribe_audio(self, recording: ZoomRecording) -> str | None:
        audio_file = recording.audio_file
        if not audio_file:
            return None
        audio = self.download_audio(audio_file.id)
        path = self.write_audio_file(recording, audio)
        return transcribe_audio_file(path, settings=self.settings)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.authenticate()}"}
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{self.api_base_url}{path}",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        return response.json()

    def download_recording_file(self, recording_id: str) -> bytes:
        download_url = self._download_urls.get(recording_id)
        if not download_url:
            raise KeyError(f"Unknown Zoom recording file ID: {recording_id}")
        headers = {"Authorization": f"Bearer {self.authenticate()}"}
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get(download_url, headers=headers)
            if response.status_code in {401, 403}:
                response = client.get(
                    with_access_token(download_url, self.authenticate()),
                    headers=headers,
                )
            response.raise_for_status()
        return response.content


def zoom_file_stem(recording: ZoomRecording) -> str:
    meeting_date = "unknown-date"
    if recording.start_time:
        try:
            meeting_date = datetime.fromisoformat(
                recording.start_time.replace("Z", "+00:00")
            ).astimezone(UTC).date().isoformat()
        except ValueError:
            meeting_date = recording.start_time[:10]
    return safe_filename(f"{meeting_date}-{recording.topic}-{recording.uuid}")


def with_access_token(url: str, access_token: str) -> str:
    parsed = urlparse(url)
    separator = "&" if parsed.query else "?"
    return f"{url}{separator}access_token={quote(access_token, safe='')}"


def list_recent_recordings(days_back: int = 7) -> list[ZoomRecording]:
    return ZoomClient().list_recent_recordings(days_back=days_back)


def download_transcript(recording_id: str) -> str:
    return ZoomClient().download_transcript(recording_id)
