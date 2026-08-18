import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.main import tribble_list, tribble_sync
from app.persistence import RunStore
from app.tribble_client import TribbleClient, TribbleMeeting


def create_tribble_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            title TEXT,
            date TEXT,
            platform TEXT,
            recording_state TEXT,
            has_summary INTEGER
        );
        CREATE TABLE meeting_details (
            meeting_id TEXT PRIMARY KEY,
            content TEXT,
            summary_text TEXT,
            user_notes_content TEXT,
            coach_call_context TEXT,
            coaching_log_summary TEXT
        );
        CREATE TABLE transcript_entries (
            meeting_id TEXT,
            seq INTEGER,
            ts INTEGER,
            speaker TEXT,
            text TEXT,
            PRIMARY KEY (meeting_id, seq)
        );
        """
    )
    connection.executemany(
        """
        INSERT INTO meetings (id, title, date, platform, recording_state, has_summary)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "meeting-100",
                "Acme Discovery",
                "2026-07-27T18:30:00.000Z",
                "zoom",
                "completed",
                1,
            ),
            (
                "meeting-200",
                "Tomorrow's Meeting",
                "2026-07-28T18:30:00.000Z",
                "teams",
                None,
                0,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO meeting_details (meeting_id, summary_text, content)
        VALUES (?, ?, ?)
        """,
        (
            "meeting-100",
            "# Participants:\n"
            "- Ryan\n"
            "- Pradeep\n\n"
            "# Summary:\n"
            "- The team agreed to schedule the workshop.\n"
            "- Ryan will send the notes by Friday.",
            "# Participants:\n"
            "* Ryan\n"
            "* Pradeep\n\n"
            "# Summary:\n"
            "* The team agreed to schedule the workshop.\n"
            "* Ryan will send the notes by Friday.\n\n"
            "# Action Items:\n"
            "* Send the notes. (Assignee: Ryan, Due Date: Friday)",
        ),
    )
    connection.executemany(
        """
        INSERT INTO transcript_entries (meeting_id, seq, ts, speaker, text)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("meeting-100", 1, 1000, "Ryan", "We agreed to schedule the workshop."),
            ("meeting-100", 2, 65000, None, "I will send the notes by Friday."),
        ],
    )
    connection.commit()
    connection.close()


def make_settings(tmp_path: Path, db_path: Path) -> Settings:
    return Settings(
        DATA_DIR=tmp_path / "runs",
        TRIBBLE_DB_PATH=db_path,
        TRIBBLE_DOWNLOAD_DIR=tmp_path / "tribble",
        TRIBBLE_TIMEZONE="America/New_York",
        DRY_RUN=True,
        OPENAI_API_KEY=None,
        SALESFORCE_CLI_ENABLED=False,
    )


def test_tribble_client_lists_and_exports_transcribed_meetings(tmp_path: Path) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)
    client = TribbleClient(settings)

    meetings = client.list_transcribed_meetings()
    transcript_path = client.write_transcript_file(meetings[0])

    assert [meeting.id for meeting in meetings] == ["meeting-100"]
    assert meetings[0].transcript_lines == 2
    assert meetings[0].recording_completed is True
    assert meetings[0].local_date("America/New_York") == "2026-07-27"
    assert transcript_path.read_text(encoding="utf-8").splitlines() == [
        "[00:00:00] Ryan: We agreed to schedule the workshop.",
        "[00:01:04] Unknown: I will send the notes by Friday.",
    ]


def test_tribble_sync_creates_one_deduplicated_review_run(tmp_path: Path) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)

    listing = tribble_list(settings=settings)
    first = tribble_sync(local_only=True, settings=settings)
    second = tribble_sync(local_only=True, settings=settings)

    assert listing["meetings"][0]["already_processed"] is False
    assert listing["meetings"][0]["ready_for_sync"] is True
    assert listing["meetings"][0]["readiness_reason"] is None
    assert len(first["processed"]) == 1
    assert first["summarizer"] == "local"
    assert first["processed"][0]["meeting_id"] == "meeting-100"
    assert first["processed"][0]["note_source"] == "tribble"
    assert second["processed"] == []
    assert second["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "already_processed"}
    ]

    runs = RunStore(settings).list()
    assert len(runs) == 1
    assert runs[0].tribble_meeting_id == "meeting-100"
    assert runs[0].intelligence.meeting_title == "Acme Discovery"
    assert runs[0].intelligence.action_items[0].owner == "Ryan"
    assert runs[0].intelligence.action_items[0].due_date == "Friday"

    runs[0].intelligence.meeting_title = "Corrected Acme QBR"
    runs[0].intelligence.customer_account_guess = "Acme"
    RunStore(settings).save(runs[0])

    refreshed = tribble_sync(local_only=True, refresh=True, settings=settings)
    assert refreshed["processed"][0]["run_id"] == runs[0].run_id
    assert refreshed["processed"][0]["refreshed"] is True
    assert len(RunStore(settings).list()) == 1
    assert RunStore(settings).list()[0].intelligence.meeting_title == "Corrected Acme QBR"
    assert RunStore(settings).list()[0].intelligence.customer_account_guess == "Acme"


def test_tribble_sync_waits_for_recording_and_summary_completion(tmp_path: Path) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE meetings SET recording_state = 'processing' WHERE id = 'meeting-100'"
        )

    listing = tribble_list(settings=settings)
    processing = tribble_sync(local_only=True, settings=settings)

    assert listing["meetings"][0]["ready_for_sync"] is False
    assert listing["meetings"][0]["readiness_reason"] == "recording_not_completed"
    assert processing["processed"] == []
    assert processing["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "recording_not_completed"}
    ]
    assert RunStore(settings).list() == []
    assert not (tmp_path / "tribble").exists()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE meetings SET recording_state = 'completed', has_summary = 0 "
            "WHERE id = 'meeting-100'"
        )

    no_summary = tribble_sync(local_only=True, settings=settings)

    assert no_summary["processed"] == []
    assert no_summary["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "summary_not_ready"}
    ]
    assert RunStore(settings).list() == []

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE meetings SET has_summary = 1 WHERE id = 'meeting-100'"
        )

    completed = tribble_sync(local_only=True, settings=settings)
    duplicate = tribble_sync(local_only=True, settings=settings)

    assert [item["meeting_id"] for item in completed["processed"]] == ["meeting-100"]
    assert duplicate["processed"] == []
    assert duplicate["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "already_processed"}
    ]
    assert len(RunStore(settings).list()) == 1


def test_tribble_sync_waits_for_parseable_summary_content(tmp_path: Path) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE meeting_details SET summary_text = '', content = '' "
            "WHERE meeting_id = 'meeting-100'"
        )

    result = tribble_sync(local_only=True, settings=settings)

    assert result["processed"] == []
    assert result["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "summary_content_not_ready"}
    ]
    assert RunStore(settings).list() == []


@pytest.mark.parametrize("state", [None, "", "processing", "finalizing", "unknown"])
def test_tribble_recording_completion_fails_closed(state: str | None) -> None:
    meeting = TribbleMeeting(
        id="meeting-100",
        title="Acme Discovery",
        date=None,
        platform=None,
        recording_state=state,
        has_summary=True,
        transcript_lines=2,
    )

    assert meeting.recording_completed is False


def test_tribble_recording_completion_normalizes_case_and_whitespace() -> None:
    meeting = TribbleMeeting(
        id="meeting-100",
        title="Acme Discovery",
        date=None,
        platform=None,
        recording_state="  CoMpLeTeD  ",
        has_summary=True,
        transcript_lines=2,
    )

    assert meeting.recording_completed is True


def test_tribble_limit_skips_newer_processing_meeting_without_using_quota(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO meetings
                (id, title, date, platform, recording_state, has_summary)
            VALUES
                ('meeting-300', 'Still Processing', '2026-07-29T18:30:00.000Z',
                 'zoom', 'processing', 0)
            """
        )
        connection.execute(
            """
            INSERT INTO transcript_entries (meeting_id, seq, ts, speaker, text)
            VALUES ('meeting-300', 1, 1000, 'Ryan', 'This transcript is partial.')
            """
        )

    result = tribble_sync(local_only=True, limit=1, settings=settings)

    assert [item["meeting_id"] for item in result["processed"]] == ["meeting-100"]
    assert result["skipped"] == [
        {"meeting_id": "meeting-300", "reason": "recording_not_completed"}
    ]


def test_tribble_refresh_cannot_bypass_completion_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "tribble.db"
    create_tribble_db(db_path)
    settings = make_settings(tmp_path, db_path)
    first = tribble_sync(local_only=True, settings=settings)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE meetings SET recording_state = 'processing' WHERE id = 'meeting-100'"
        )

    refresh = tribble_sync(local_only=True, refresh=True, settings=settings)

    assert refresh["processed"] == []
    assert refresh["skipped"] == [
        {"meeting_id": "meeting-100", "reason": "recording_not_completed"}
    ]
    assert RunStore(settings).list()[0].run_id == first["processed"][0]["run_id"]
