import sqlite3
from pathlib import Path

from app.config import Settings
from app.main import tribble_list, tribble_sync
from app.persistence import RunStore
from app.tribble_client import TribbleClient


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

    refreshed = tribble_sync(local_only=True, refresh=True, settings=settings)
    assert refreshed["processed"][0]["run_id"] == runs[0].run_id
    assert refreshed["processed"][0]["refreshed"] is True
    assert len(RunStore(settings).list()) == 1
