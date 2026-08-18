import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.models import ActionItem, Attendee, MeetingIntelligence, safe_filename

SECTION_RE = re.compile(
    r"^# (Participants|Summary|Action Items|Call Objectives|Call Score):\s*$",
    re.MULTILINE,
)
BULLET_RE = re.compile(r"^[*-]\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class TribbleMeeting:
    id: str
    title: str
    date: str | None
    platform: str | None
    recording_state: str | None
    has_summary: bool
    transcript_lines: int

    @property
    def recording_completed(self) -> bool:
        return (self.recording_state or "").strip().casefold() == "completed"

    def local_date(self, timezone_name: str) -> str | None:
        if not self.date:
            return None
        try:
            parsed = datetime.fromisoformat(self.date.replace("Z", "+00:00"))
            return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            return self.date[:10] or None


@dataclass(frozen=True)
class TribbleTranscriptEntry:
    seq: int
    timestamp_ms: int
    speaker: str
    text: str


@dataclass(frozen=True)
class TribbleMeetingDetails:
    summary_text: str
    content: str
    user_notes_content: str


class TribbleClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.db_path = Path(self.settings.tribble_db_path).expanduser()

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    def list_transcribed_meetings(
        self,
        meeting_id: str | None = None,
        limit: int | None = None,
    ) -> list[TribbleMeeting]:
        query = """
            SELECT
                m.id,
                COALESCE(NULLIF(TRIM(m.title), ''), 'Untitled Tribble Meeting') AS title,
                m.date,
                m.platform,
                m.recording_state,
                COALESCE(m.has_summary, 0) AS has_summary,
                COUNT(t.seq) AS transcript_lines
            FROM meetings m
            JOIN transcript_entries t ON t.meeting_id = m.id
            WHERE (? IS NULL OR m.id = ?)
            GROUP BY m.id, m.title, m.date, m.platform, m.recording_state, m.has_summary
            HAVING COUNT(t.seq) > 0
            ORDER BY m.date DESC
        """
        parameters: list[str | int | None] = [meeting_id, meeting_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()

        return [
            TribbleMeeting(
                id=str(row["id"]),
                title=str(row["title"]),
                date=str(row["date"]) if row["date"] else None,
                platform=str(row["platform"]) if row["platform"] else None,
                recording_state=(
                    str(row["recording_state"]) if row["recording_state"] else None
                ),
                has_summary=bool(row["has_summary"]),
                transcript_lines=int(row["transcript_lines"]),
            )
            for row in rows
        ]

    def transcript_entries(self, meeting_id: str) -> list[TribbleTranscriptEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    seq,
                    ts,
                    COALESCE(NULLIF(TRIM(speaker), ''), 'Unknown') AS speaker,
                    COALESCE(text, '') AS text
                FROM transcript_entries
                WHERE meeting_id = ?
                ORDER BY seq
                """,
                (meeting_id,),
            ).fetchall()

        return [
            TribbleTranscriptEntry(
                seq=int(row["seq"]),
                timestamp_ms=int(row["ts"]),
                speaker=str(row["speaker"]),
                text=str(row["text"]),
            )
            for row in rows
        ]

    def meeting_details(self, meeting_id: str) -> TribbleMeetingDetails:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(summary_text, '') AS summary_text,
                    COALESCE(content, '') AS content,
                    COALESCE(user_notes_content, '') AS user_notes_content
                FROM meeting_details
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()

        if row is None:
            return TribbleMeetingDetails("", "", "")
        return TribbleMeetingDetails(
            summary_text=str(row["summary_text"]),
            content=str(row["content"]),
            user_notes_content=str(row["user_notes_content"]),
        )

    def structured_intelligence(
        self,
        meeting: TribbleMeeting,
    ) -> MeetingIntelligence | None:
        details = self.meeting_details(meeting.id)
        summary_sections = split_sections(details.summary_text)
        content_sections = split_sections(details.content)
        summary_points = section_bullets(
            summary_sections.get("Summary") or content_sections.get("Summary", "")
        )
        if not summary_points:
            return None

        participant_values = section_bullets(
            summary_sections.get("Participants")
            or content_sections.get("Participants", "")
        )
        attendees = [
            Attendee(name=re.sub(r"\s+\([^)]*\)\s*$", "", value).strip() or None)
            for value in participant_values
        ]
        actions = parse_action_items(
            content_sections.get("Action Items")
            or summary_sections.get("Action Items", "")
        )
        executive_summary = " ".join(summary_points[:3])
        proposed_next_step = (
            actions[0].task
            if actions
            else "Review the recap and confirm the next steps with attendees."
        )

        return MeetingIntelligence(
            meeting_title=meeting.title,
            meeting_date=meeting.local_date(self.settings.tribble_timezone),
            attendees=attendees,
            executive_summary=executive_summary,
            key_points=summary_points,
            action_items=actions,
            proposed_next_step=proposed_next_step,
            follow_up_email_draft=build_follow_up_email(
                meeting.title,
                summary_points,
                actions,
            ),
        )

    def ready_intelligence(
        self,
        meeting: TribbleMeeting,
    ) -> tuple[MeetingIntelligence | None, str | None]:
        """Return Tribble intelligence only after recording and summary processing finish."""
        if not meeting.recording_completed:
            return None, "recording_not_completed"
        if not meeting.has_summary:
            return None, "summary_not_ready"

        intelligence = self.structured_intelligence(meeting)
        if intelligence is None:
            return None, "summary_content_not_ready"
        return intelligence, None

    def write_transcript_file(self, meeting: TribbleMeeting) -> Path:
        entries = self.transcript_entries(meeting.id)
        if not entries:
            raise ValueError(f"Tribble meeting has no transcript entries: {meeting.id}")

        output_dir = Path(self.settings.tribble_download_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        day = meeting.local_date(self.settings.tribble_timezone) or "undated"
        title_slug = safe_filename(meeting.title).strip("-_.")[:80] or "tribble-meeting"
        id_slug = safe_filename(meeting.id).strip("-_.") or "unknown-id"
        path = output_dir / f"{day}_{title_slug}_{id_slug}.txt"

        t0 = min(entry.timestamp_ms for entry in entries)
        lines = []
        for entry in entries:
            elapsed = max(0, (entry.timestamp_ms - t0) // 1000)
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            text = " ".join(entry.text.replace("\r", "\n").splitlines()).strip()
            lines.append(
                f"[{hours:02d}:{minutes:02d}:{seconds:02d}] "
                f"{entry.speaker}: {text}"
            )

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _connect(self) -> sqlite3.Connection:
        if not self.available:
            raise FileNotFoundError(f"Tribble database not found: {self.db_path}")

        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection


def split_sections(value: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(value))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        body_start = value.find("\n", match.start())
        if body_start < 0:
            body_start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        sections[match.group(1)] = value[body_start:end].strip()
    return sections


def section_bullets(value: str) -> list[str]:
    return [" ".join(match.group(1).split()) for match in BULLET_RE.finditer(value)]


def parse_action_items(value: str) -> list[ActionItem]:
    items: list[ActionItem] = []
    for bullet in section_bullets(value):
        marker = " (Assignee:"
        if marker not in bullet or not bullet.endswith(")"):
            items.append(ActionItem(task=bullet))
            continue

        task, metadata = bullet.rsplit(marker, 1)
        metadata = metadata[:-1].strip()
        owner = metadata
        due_date = None
        due_marker = ", Due Date:"
        if due_marker in metadata:
            owner, due_date = metadata.split(due_marker, 1)
        items.append(
            ActionItem(
                owner=owner.strip() or None,
                task=task.strip(),
                due_date=due_date.strip() if due_date else None,
            )
        )
    return items


def build_follow_up_email(
    title: str,
    summary_points: list[str],
    actions: list[ActionItem],
) -> str:
    recap = "\n".join(f"- {point}" for point in summary_points[:5])
    action_lines = (
        "\n".join(
            f"- {item.task} (Owner: {item.owner or 'Unknown'}, "
            f"Due: {item.due_date or 'Not specified'})"
            for item in actions
        )
        or "- Confirm next steps and owners."
    )
    return (
        f"Subject: Follow-up from {title}\n\n"
        "Hi everyone,\n\n"
        "Thanks for the discussion. Here is a quick recap:\n"
        f"{recap}\n\n"
        "Action items:\n"
        f"{action_lines}\n\n"
        "Best,\n"
    )
