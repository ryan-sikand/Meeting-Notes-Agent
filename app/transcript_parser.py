import re
from pathlib import Path

from app.models import MeetingMetadata, ParsedTranscript, TranscriptSegment, resolve_path

SUPPORTED_EXTENSIONS = {".txt", ".vtt"}
TIMESTAMP_RE = re.compile(
    r"(?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?"
    r"(?:\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)?)"
)
SPEAKER_RE = re.compile(r"^(?P<speaker>[A-Z][\w .,'-]{0,60}?):\s*(?P<text>.+)$")
TIMESTAMPED_SPEAKER_RE = re.compile(
    r"^\[(?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\]\s*"
    r"(?P<speaker>[A-Z][\w .,'-]{0,60}?):\s*(?P<text>.+)$"
)
STANDALONE_SPEAKER_RE = re.compile(r"^(Speaker\s+\d+)$")
DATE_RE = re.compile(r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})")


def parse_transcript(path: str | Path, metadata: MeetingMetadata | None = None) -> ParsedTranscript:
    source_path = resolve_path(path)
    if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"Unsupported transcript extension. Expected one of: {allowed}")

    raw_text = source_path.read_text(encoding="utf-8-sig")
    parsed_metadata = metadata or metadata_from_filename(source_path)
    parsed_metadata.source_filename = source_path.name
    segments = parse_vtt(raw_text) if source_path.suffix.lower() == ".vtt" else parse_txt(raw_text)
    normalized_text = "\n".join(format_segment(segment) for segment in segments).strip()

    return ParsedTranscript(
        source_path=str(source_path),
        filename=source_path.name,
        raw_text=raw_text,
        normalized_text=normalized_text or raw_text.strip(),
        segments=segments,
        metadata=parsed_metadata,
    )


def parse_txt(raw_text: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    current_timestamp: str | None = None
    current_speaker: str | None = None

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        timestamped_speaker_match = TIMESTAMPED_SPEAKER_RE.match(stripped)
        if timestamped_speaker_match:
            current_speaker = normalize_speaker(timestamped_speaker_match.group("speaker"))
            segments.append(
                TranscriptSegment(
                    speaker=current_speaker,
                    timestamp=timestamped_speaker_match.group("timestamp"),
                    text=timestamped_speaker_match.group("text").strip(),
                )
            )
            current_timestamp = None
            continue

        standalone_speaker_match = STANDALONE_SPEAKER_RE.match(stripped)
        if standalone_speaker_match and not looks_like_content(stripped):
            current_speaker = normalize_speaker(standalone_speaker_match.group(1))
            current_timestamp = None
            continue

        timestamp_match = TIMESTAMP_RE.match(stripped)
        if timestamp_match and timestamp_match.group("timestamp") == stripped:
            current_timestamp = stripped
            continue

        speaker_match = SPEAKER_RE.match(stripped)
        if speaker_match:
            current_speaker = normalize_speaker(speaker_match.group("speaker"))
            segments.append(
                TranscriptSegment(
                    speaker=current_speaker,
                    timestamp=current_timestamp,
                    text=speaker_match.group("text").strip(),
                )
            )
            current_timestamp = None
        elif (
            segments
            and segments[-1].speaker == current_speaker
            and not current_timestamp
            and should_join_fragment(stripped)
        ):
            segments[-1].text = f"{segments[-1].text} {stripped}"
        elif current_speaker:
            segments.append(
                TranscriptSegment(
                    speaker=current_speaker,
                    timestamp=current_timestamp,
                    text=stripped,
                )
            )
            current_timestamp = None
        else:
            segments.append(TranscriptSegment(timestamp=current_timestamp, text=stripped))
            current_timestamp = None

    return segments


def parse_vtt(raw_text: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    pending_timestamp: str | None = None
    pending_lines: list[str] = []

    def flush() -> None:
        nonlocal pending_timestamp, pending_lines
        if not pending_lines:
            pending_timestamp = None
            return
        text = " ".join(pending_lines).strip()
        speaker = None
        speaker_match = SPEAKER_RE.match(text)
        if speaker_match:
            speaker = normalize_speaker(speaker_match.group("speaker"))
            text = speaker_match.group("text").strip()
        segments.append(TranscriptSegment(speaker=speaker, timestamp=pending_timestamp, text=text))
        pending_timestamp = None
        pending_lines = []

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.upper() == "WEBVTT" or stripped.isdigit():
            continue
        timestamp_match = TIMESTAMP_RE.search(stripped)
        if timestamp_match:
            flush()
            pending_timestamp = timestamp_match.group("timestamp")
            continue
        pending_lines.append(remove_vtt_tags(stripped))

    flush()
    return segments


def metadata_from_filename(path: Path) -> MeetingMetadata:
    stem = path.stem
    date_match = DATE_RE.search(stem)
    meeting_date = None
    if date_match:
        year, month, day = date_match.groups()
        meeting_date = f"{year}-{int(month):02d}-{int(day):02d}"

    title = DATE_RE.sub("", stem).replace("_", " ").replace("-", " ")
    title = " ".join(title.split()) or stem
    return MeetingMetadata(title=title, meeting_date=meeting_date, source_filename=path.name)


def normalize_speaker(value: str) -> str:
    return " ".join(value.strip().split())


def looks_like_content(value: str) -> bool:
    lower = value.lower().strip(".,?!")
    content_words = {
        "yes",
        "no",
        "okay",
        "fantastic",
        "morning",
        "thanks",
        "thank you",
        "correct",
    }
    return lower in content_words or value.endswith((".", "?", "!"))


def should_join_fragment(value: str) -> bool:
    return not STANDALONE_SPEAKER_RE.match(value) or looks_like_content(value)


def remove_vtt_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def format_segment(segment: TranscriptSegment) -> str:
    prefix_parts = []
    if segment.timestamp:
        prefix_parts.append(f"[{segment.timestamp}]")
    if segment.speaker:
        prefix_parts.append(f"{segment.speaker}:")
    prefix = " ".join(prefix_parts)
    return f"{prefix} {segment.text}".strip()
