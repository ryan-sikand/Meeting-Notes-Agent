from pathlib import Path

from app.transcript_parser import parse_transcript


def test_parse_txt_normalizes_speakers_and_timestamps() -> None:
    path = Path("tests/fixtures/sample_transcript.txt")

    parsed = parse_transcript(path)

    assert parsed.filename == "sample_transcript.txt"
    assert parsed.metadata.title == "sample transcript"
    assert parsed.segments[0].text == "2026-06-09 Acme Discovery"
    assert any(segment.speaker == "Sarah Connor" for segment in parsed.segments)
    assert "[00:00:01] Sarah Connor:" in parsed.normalized_text


def test_parse_vtt_removes_webvtt_markup(tmp_path: Path) -> None:
    path = tmp_path / "2026-06-09-acme-discovery.vtt"
    path.write_text(
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:03.000\n"
        "<v Sarah Connor>Sarah Connor: Hello team</v>\n\n",
        encoding="utf-8",
    )

    parsed = parse_transcript(path)

    assert parsed.metadata.meeting_date == "2026-06-09"
    assert parsed.segments[0].speaker == "Sarah Connor"
    assert parsed.segments[0].text == "Hello team"


def test_parse_txt_groups_zoom_standalone_speaker_blocks(tmp_path: Path) -> None:
    path = tmp_path / "zoom-paste.txt"
    path.write_text(
        "Speaker 1\n"
        "10:03:28\n"
        "So one challenge is data aggregation.\n"
        "We need a dashboard.\n"
        "\n"
        "Speaker 2\n"
        "10:04:00\n"
        "Can you share the source systems?\n",
        encoding="utf-8",
    )

    parsed = parse_transcript(path)

    assert parsed.segments[0].speaker == "Speaker 1"
    assert parsed.segments[0].timestamp == "10:03:28"
    assert parsed.segments[0].text == "So one challenge is data aggregation. We need a dashboard."
    assert parsed.segments[1].speaker == "Speaker 2"
    assert "[10:04:00] Speaker 2: Can you share the source systems?" in parsed.normalized_text


def test_parse_txt_preserves_inline_timestamped_speaker_turns(tmp_path: Path) -> None:
    path = tmp_path / "tribble.txt"
    path.write_text(
        "[00:00:00] Ryan Sikand: We agreed to schedule the workshop.\n"
        "[00:01:04] Pradeep Paruchuri: I will send the notes by Friday.\n",
        encoding="utf-8",
    )

    parsed = parse_transcript(path)

    assert len(parsed.segments) == 2
    assert parsed.segments[0].speaker == "Ryan Sikand"
    assert parsed.segments[0].timestamp == "00:00:00"
    assert parsed.segments[1].speaker == "Pradeep Paruchuri"
    assert parsed.normalized_text == (
        "[00:00:00] Ryan Sikand: We agreed to schedule the workshop.\n"
        "[00:01:04] Pradeep Paruchuri: I will send the notes by Friday."
    )
