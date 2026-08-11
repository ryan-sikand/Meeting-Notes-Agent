from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.config import Settings, get_settings
from app.models import MeetingRun, RunStatus


class RunStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = Path(self.settings.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, run: MeetingRun) -> MeetingRun:
        self.save(run)
        return run

    def save(self, run: MeetingRun) -> None:
        path = self.path_for(run.run_id)
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    def get(self, run_id: str) -> MeetingRun:
        path = self.path_for(run_id)
        if not path.exists():
            raise FileNotFoundError(f"Run not found: {run_id}")
        return MeetingRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[MeetingRun]:
        runs = [
            MeetingRun.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
        ]
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

    def update_status(self, run_id: str, status: RunStatus) -> MeetingRun:
        run = self.get(run_id)
        run.status = status
        self.save(run)
        return run

    def has_zoom_meeting_uuid(self, meeting_uuid: str) -> bool:
        return any(run.zoom_meeting_uuid == meeting_uuid for run in self.list())

    def has_tribble_meeting_id(self, meeting_id: str) -> bool:
        return self.find_by_tribble_meeting_id(meeting_id) is not None

    def find_by_tribble_meeting_id(self, meeting_id: str) -> MeetingRun | None:
        return next(
            (run for run in self.list() if run.tribble_meeting_id == meeting_id),
            None,
        )

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"


def new_run_id() -> str:
    return uuid4().hex[:12]


def now() -> datetime:
    return datetime.now().astimezone()
