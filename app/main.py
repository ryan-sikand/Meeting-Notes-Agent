import argparse
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.approval_routes import router
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.models import MeetingIntelligence, MeetingMetadata, MeetingRun
from app.note_generator import generate_quip_note
from app.openai_client import summarize_meeting
from app.opportunity_matcher import match_opportunity
from app.persistence import RunStore, new_run_id, now
from app.salesforce_client import SalesforceClient, build_proposed_updates
from app.transcript_parser import parse_transcript
from app.tribble_client import TribbleClient
from app.zoom_client import ZoomClient, ZoomRecording
from app.zoom_docs_client import ZoomDocsClient, ZoomDocsContentUnavailable, is_zoom_docs_url

LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Meeting Notes Agent")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)


def process_transcript(
    transcript_path: str | Path,
    settings: Settings | None = None,
    zoom_meeting_uuid: str | None = None,
    tribble_meeting_id: str | None = None,
    metadata: MeetingMetadata | None = None,
    intelligence: MeetingIntelligence | None = None,
    run_id: str | None = None,
) -> MeetingRun:
    settings = settings or get_settings()
    parsed = parse_transcript(transcript_path, metadata=metadata)
    if settings.log_transcripts:
        LOGGER.info("Parsed transcript %s with %s segments", parsed.filename, len(parsed.segments))
    else:
        LOGGER.info("Parsed transcript metadata for %s", parsed.filename)

    intelligence = intelligence or summarize_meeting(
        parsed.normalized_text,
        parsed.metadata,
        settings=settings,
    )
    candidates = SalesforceClient(settings).find_candidates(intelligence)
    opportunity_match = match_opportunity(
        intelligence=intelligence,
        candidates=candidates,
        transcript_text=parsed.normalized_text,
    )
    note = generate_quip_note(intelligence, opportunity_match)
    run = MeetingRun(
        run_id=run_id or new_run_id(),
        created_at=now(),
        transcript_path=parsed.source_path,
        transcript_filename=parsed.filename,
        zoom_meeting_uuid=zoom_meeting_uuid,
        tribble_meeting_id=tribble_meeting_id,
        intelligence=intelligence,
        salesforce_match=opportunity_match,
        proposed_salesforce_updates=build_proposed_updates(intelligence),
        quip_note=note,
    )
    RunStore(settings).create(run)
    return run


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="meeting-notes-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    process_parser = subparsers.add_parser(
        "process",
        help="Process a transcript and create a review run.",
    )
    process_parser.add_argument("transcript_path")

    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help="Process a transcript with DRY_RUN forced on.",
    )
    dry_run_parser.add_argument("transcript_path")

    zoom_sync_parser = subparsers.add_parser(
        "zoom-sync",
        help="Ingest recent Zoom cloud recordings with transcript files.",
    )
    zoom_sync_parser.add_argument("--days-back", type=int, default=7)

    tribble_list_parser = subparsers.add_parser(
        "tribble-list",
        help="List meetings with transcripts in the local Tribble Scribe database.",
    )
    tribble_list_parser.add_argument("--limit", type=int, default=20)

    tribble_sync_parser = subparsers.add_parser(
        "tribble-sync",
        help=(
            "Create review runs only after Tribble finishes the recording and "
            "structured summary."
        ),
    )
    tribble_sync_parser.add_argument("--meeting-id")
    tribble_sync_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of new meetings to process, newest first.",
    )
    tribble_sync_parser.add_argument(
        "--local",
        action="store_true",
        help="Do not send transcripts to OpenAI; use the local summarizer.",
    )
    tribble_sync_parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Regenerate an existing Tribble review run in place after the same "
            "completion checks pass."
        ),
    )

    subparsers.add_parser(
        "zoom-auth-check",
        help="Validate Zoom Server-to-Server OAuth credentials and show token metadata.",
    )
    subparsers.add_parser(
        "salesforce-auth-check",
        help="Validate Salesforce credentials and confirm read/query access.",
    )

    zoom_doc_parser = subparsers.add_parser(
        "zoom-doc",
        help="Inspect a shared Zoom Docs URL and process it only if text is embedded.",
    )
    zoom_doc_parser.add_argument("url")

    subparsers.add_parser("review", help="Start the local FastAPI review UI.")
    subparsers.add_parser("list-runs", help="List locally stored review runs.")

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "process":
        run = process_transcript(args.transcript_path, settings)
        print_run_created(run)
    elif args.command == "dry-run":
        settings.dry_run = True
        run = process_transcript(args.transcript_path, settings)
        print_run_created(run)
    elif args.command == "review":
        uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
    elif args.command == "list-runs":
        runs = RunStore(settings).list()
        print(json.dumps([run_summary(run) for run in runs], indent=2))
    elif args.command == "zoom-sync":
        result = zoom_sync(days_back=args.days_back, settings=settings)
        print(json.dumps(result, indent=2))
    elif args.command == "tribble-list":
        result = tribble_list(limit=args.limit, settings=settings)
        print(json.dumps(result, indent=2))
    elif args.command == "tribble-sync":
        result = tribble_sync(
            meeting_id=args.meeting_id,
            limit=args.limit,
            local_only=args.local,
            refresh=args.refresh,
            settings=settings,
        )
        print(json.dumps(result, indent=2))
    elif args.command == "zoom-auth-check":
        result = zoom_auth_check(settings=settings)
        print(json.dumps(result, indent=2))
    elif args.command == "salesforce-auth-check":
        result = SalesforceClient(settings).auth_check()
        print(json.dumps(result, indent=2))
    elif args.command == "zoom-doc":
        result = process_zoom_doc_url(args.url, settings=settings)
        print(json.dumps(result, indent=2))


def print_run_created(run: MeetingRun) -> None:
    settings = get_settings()
    print(f"Created run {run.run_id}")
    print(f"Review URL: {settings.review_base_url.rstrip('/')}/review/{run.run_id}")


def run_summary(run: MeetingRun) -> dict[str, str | int | None]:
    return {
        "run_id": run.run_id,
        "created_at": run.created_at.isoformat(),
        "status": run.status.value,
        "transcript_filename": run.transcript_filename,
        "account_name": run.salesforce_match.account_name,
        "opportunity_name": run.salesforce_match.opportunity_name,
        "match_status": run.salesforce_match.match_status,
        "confidence": run.salesforce_match.confidence,
        "zoom_meeting_uuid": run.zoom_meeting_uuid,
        "tribble_meeting_id": run.tribble_meeting_id,
    }


def zoom_sync(days_back: int = 7, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    store = RunStore(settings)
    client = ZoomClient(settings)
    processed: list[str] = []
    skipped: list[dict[str, str]] = []

    for recording in client.list_recent_recordings(days_back=days_back):
        if store.has_zoom_meeting_uuid(recording.uuid):
            skipped.append({"uuid": recording.uuid, "reason": "already_processed"})
            continue

        transcript_text = transcript_text_for_recording(client, recording, settings)
        if transcript_text is None:
            skipped.append({"uuid": recording.uuid, "reason": "missing_transcript"})
            continue

        transcript_path = client.write_transcript_file(recording, transcript_text)
        run = process_transcript(
            transcript_path,
            settings=settings,
            zoom_meeting_uuid=recording.uuid,
        )
        processed.append(run.run_id)

    return {"processed_run_ids": processed, "skipped": skipped}


def zoom_auth_check(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    client = ZoomClient(settings)
    if not client.configured:
        return {"configured": False, "message": "Zoom credentials are not configured."}
    client.authenticate()
    assert client.token is not None
    return {
        "configured": True,
        "api_base_url": client.api_base_url,
        "expires_at": client.token.expires_at.isoformat(),
        "scopes": client.token.scopes,
    }


def tribble_list(
    limit: int = 20,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    client = TribbleClient(settings)
    meetings = client.list_transcribed_meetings(limit=limit)
    store = RunStore(settings)
    readiness = {
        meeting.id: client.ready_intelligence(meeting)[1] for meeting in meetings
    }
    return {
        "database": str(client.db_path),
        "meetings": [
            {
                "id": meeting.id,
                "title": meeting.title,
                "date": meeting.local_date(settings.tribble_timezone),
                "platform": meeting.platform,
                "recording_state": meeting.recording_state,
                "has_summary": meeting.has_summary,
                "transcript_lines": meeting.transcript_lines,
                "ready_for_sync": readiness[meeting.id] is None,
                "readiness_reason": readiness[meeting.id],
                "already_processed": store.has_tribble_meeting_id(meeting.id),
            }
            for meeting in meetings
        ],
    }


def tribble_sync(
    meeting_id: str | None = None,
    limit: int | None = None,
    local_only: bool = False,
    refresh: bool = False,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    if local_only:
        settings = settings.model_copy(update={"openai_api_key": None})
    client = TribbleClient(settings)
    store = RunStore(settings)
    meetings = client.list_transcribed_meetings(meeting_id=meeting_id)
    processed: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []

    if meeting_id and not meetings:
        skipped.append(
            {
                "meeting_id": meeting_id,
                "reason": "not_found_or_not_transcribed",
            }
        )

    for meeting in meetings:
        tribble_intelligence, readiness_reason = client.ready_intelligence(meeting)
        if readiness_reason:
            skipped.append(
                {
                    "meeting_id": meeting.id,
                    "reason": readiness_reason,
                }
            )
            continue

        existing_run = store.find_by_tribble_meeting_id(meeting.id)
        if existing_run and not refresh:
            skipped.append(
                {
                    "meeting_id": meeting.id,
                    "reason": "already_processed",
                }
            )
            continue
        if limit is not None and len(processed) >= limit:
            break

        transcript_path = client.write_transcript_file(meeting)
        structured_intelligence = tribble_intelligence if local_only else None
        if structured_intelligence is not None and existing_run is not None:
            preserved = {
                "meeting_title": existing_run.intelligence.meeting_title,
            }
            if (
                existing_run.intelligence.customer_account_guess
                and not structured_intelligence.customer_account_guess
            ):
                preserved["customer_account_guess"] = (
                    existing_run.intelligence.customer_account_guess
                )
            structured_intelligence = structured_intelligence.model_copy(
                update=preserved
            )
        run = process_transcript(
            transcript_path,
            settings=settings,
            tribble_meeting_id=meeting.id,
            metadata=MeetingMetadata(
                title=meeting.title,
                meeting_date=meeting.local_date(settings.tribble_timezone),
                source_filename=transcript_path.name,
            ),
            intelligence=structured_intelligence,
            run_id=existing_run.run_id if existing_run else None,
        )
        processed.append(
            {
                "meeting_id": meeting.id,
                "title": meeting.title,
                "run_id": run.run_id,
                "refreshed": existing_run is not None,
                "note_source": (
                    "tribble"
                    if structured_intelligence is not None
                    else "local" if local_only else "openai"
                ),
                "review_url": (
                    f"{settings.review_base_url.rstrip('/')}/review/{run.run_id}"
                ),
                "salesforce_match": {
                    "status": run.salesforce_match.match_status,
                    "account": run.salesforce_match.account_name,
                    "opportunity": run.salesforce_match.opportunity_name,
                    "confidence": run.salesforce_match.confidence,
                },
            }
        )

    return {
        "database": str(client.db_path),
        "summarizer": "local" if local_only or not settings.openai_api_key else "openai",
        "processed": processed,
        "skipped": skipped,
    }


def transcript_text_for_recording(
    client: ZoomClient,
    recording: ZoomRecording,
    settings: Settings,
) -> str | None:
    transcript_file = recording.transcript_file
    if transcript_file:
        return client.download_transcript(transcript_file.id)
    if not settings.transcribe_audio:
        return None
    return client.transcribe_audio(recording)


def process_zoom_doc_url(url: str, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    if not is_zoom_docs_url(url):
        raise ValueError(f"Not a supported Zoom Docs URL: {url}")

    client = ZoomDocsClient()
    inspection = client.inspect(url)
    text = inspection["content_text"]
    if not text:
        exc = ZoomDocsContentUnavailable(
            "Zoom Docs link returned only the web app shell. The document content requires "
            "authenticated Zoom Docs APIs or browser-session export access."
        )
        return {
            "processed": False,
            "reason": "content_unavailable",
            "message": str(exc),
            "inspection": {
                key: value
                for key, value in inspection.items()
                if key != "content_text"
            },
        }

    settings.zoom_download_dir.mkdir(parents=True, exist_ok=True)
    path = settings.zoom_download_dir / f"zoom-doc-{inspection['doc_id']}.txt"
    path.write_text(str(text), encoding="utf-8")
    run = process_transcript(path, settings=settings, zoom_meeting_uuid=str(inspection["doc_id"]))
    return {"processed": True, "run_id": run.run_id, "path": str(path)}


if __name__ == "__main__":
    main()
