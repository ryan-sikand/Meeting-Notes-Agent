# Meeting Notes Agent

Production-minded MVP for turning Zoom or Tribble Scribe transcripts into structured meeting
notes, a likely Salesforce Opportunity match, and a human approval page before any Salesforce
write.

## Setup

```powershell
uv sync
Copy-Item .env.example .env
```

Fill in `.env` as needed. `DRY_RUN=true` is the default and writes Quip HTML to `./out` instead of calling Quip.

If you do not have Zoom API app permissions, open the review UI and paste copied Zoom transcript text into the Paste Transcript form.
If OpenAI is not configured or returns an insufficient-quota error, the app falls back to a local deterministic meeting recap so demos can still run.

## Commands

```powershell
uv run python -m app.main dry-run .\tests\fixtures\sample_transcript.txt
uv run python -m app.main review
uv run python -m app.main list-runs
uv run python -m app.main zoom-auth-check
uv run python -m app.main zoom-sync --days-back 7
uv run python -m app.main tribble-list
uv run python -m app.main tribble-sync --local
uv run python -m app.main zoom-doc "https://hub.zoom.us/doc/..."
uv run python -m app.main process .\transcripts\acme-discovery.vtt
```

Open the printed review URL, or go to `http://127.0.0.1:8000`.

## Environment

Required integrations depend on the workflow: `OPENAI_API_KEY` for remote summarization,
Salesforce authentication for CRM matching, and `QUIP_ACCESS_TOKEN` only for Quip writes.
`OPENAI_FALLBACK_TO_LOCAL=true` keeps the app usable when OpenAI quota is unavailable.

Zoom cloud sync uses a Server-to-Server OAuth app:

- `ZOOM_ACCOUNT_ID`
- `ZOOM_CLIENT_ID`
- `ZOOM_CLIENT_SECRET`
- `TRANSCRIBE_AUDIO=false`
- `ZOOM_DOWNLOAD_DIR=./data/zoom`

The Zoom Server-to-Server OAuth app needs cloud recording read scopes for the users whose recordings you sync. Set `ZOOM_USER_ID=me` for the app owner or a specific Zoom user ID/email that the app can read.

Security defaults:

- `DRY_RUN=true`
- `LOG_TRANSCRIPTS=false`
- `REVIEW_BASE_URL=http://127.0.0.1:8000`
- local run records under `./data/runs`
- generated Quip dry-run HTML under `./out`

## Salesforce Assumptions

The agent queries Contacts, Accounts, and open Opportunities. It can use either Connected App
password-flow credentials or an existing Salesforce CLI login. For the CLI connection, set
`SALESFORCE_CLI_ENABLED=true` and `SALESFORCE_CLI_ALIAS=uipath` after running
`sf org login web --alias uipath --set-default`.

Matching uses external attendee emails, account and opportunity names, Salesforce reference
numbers, ownership, and close-date relevance. High-confidence unique matches are assigned;
ambiguous results are marked account-only or needs-review. Meetings without useful CRM signals
remain unmatched. Transcript text stays local: only extracted names and attendee emails are used
to discover Salesforce candidates.

Salesforce writes are never automatic. The CLI-backed connection is read-only in this agent.
Legacy Connected App writes remain guarded by explicit approval and `DRY_RUN=false`.

Run `./scripts/setup_salesforce.ps1` to enter Connected App credentials locally and validate read access. The script keeps `DRY_RUN=true` and stores credentials only in the git-ignored `.env` file. Recheck the connection later with `uv run python -m app.main salesforce-auth-check`.

## Quip

`create_document()` and `append_to_document()` support Quip HTML content. In dry-run mode, the app writes the generated HTML locally.

## Zoom Plan

The app accepts local `.txt` and `.vtt` transcripts and can sync recent Zoom cloud recordings:

```powershell
uv run python -m app.main zoom-sync --days-back 7
```

Zoom sync stores meeting UUIDs on created runs and skips meetings already processed. Meetings without transcript files are skipped unless `TRANSCRIBE_AUDIO=true`. When enabled, the app downloads the meeting audio and sends it to OpenAI speech-to-text before creating a review run.

Shared Zoom Docs links such as `https://hub.zoom.us/doc/...` are not the same as Zoom cloud recordings. The `zoom-doc` command inspects those links and processes embedded text if Zoom exposes it in the public page. If the link only returns the Zoom Docs web app shell, the command returns `content_unavailable`; authenticated Zoom Docs export access is required.

## Tribble Scribe on Windows

`tribble-list` reads the local Tribble database without modifying it. On Windows, the default
location is `%APPDATA%\Tribble Desktop\tribble.db`.

`tribble-sync --local` creates review runs without sending transcript text to an external
service. It skips Tribble meeting IDs already stored in `data/runs`. Use `--limit 1` for only
the newest unprocessed meeting or `--meeting-id <id>` for a specific meeting. Omit `--local`
only when sending transcript text through the configured OpenAI API is explicitly intended.
Use `--refresh` to regenerate existing Tribble drafts in place after note-generation changes.

Codex uses its bundled document runtime to run `python -m app.sharepoint_docx_export`.
The command writes Word documents under `out/sharepoint-docx`; `--run-id <id>` exports a
selected run. The personal workflow uploads them to
`My files/Meetings/Tribble Meeting Notes` without creating sharing links.

## Validation

```powershell
uv run pytest
uv run ruff check .
```

Known limitations: OpenAI extraction falls back to a deterministic demo summary when `OPENAI_API_KEY` is missing, Salesforce matching depends on query results, and the review UI is intentionally simple for MVP demos.
