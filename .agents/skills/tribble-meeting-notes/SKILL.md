---
name: tribble-meeting-notes
description: Sync completed Tribble Scribe transcripts from the local Windows Tribble Desktop database into the Meeting Notes Agent, creating deduplicated structured meeting-note review runs. Use when the user mentions Tribble, Scribe, recent transcribed meetings, syncing meeting transcripts, creating notes from recorded meetings, finding the latest meeting recap, or reviewing Tribble-generated notes.
---

# Tribble Meeting Notes

Run all commands from the `Meeting-Notes-Agent` project root. Use `uv`; do not call
`sqlite3` directly and never read `tribble_tokens.json`.

## Workflow

1. Discover available transcribed meetings:

   ```powershell
   uv run python -m app.main tribble-list
   ```

2. Choose the sync scope:

   - Latest unprocessed meeting:
     `uv run python -m app.main tribble-sync --local --limit 1`
   - All unprocessed meetings: `uv run python -m app.main tribble-sync --local`
   - One meeting:
     `uv run python -m app.main tribble-sync --local --meeting-id <id>`
   - Regenerate existing drafts:
     `uv run python -m app.main tribble-sync --local --refresh`

3. Report each created run with its meeting title, run ID, and review URL. Also report
   skipped meetings and their reasons.

4. When the user asks to inspect a generated note inline, read the matching JSON under
   `data/runs/<run-id>.json` and present `intelligence`, action items, and the proposed
   next step. Do not expose the full raw transcript unless explicitly requested.

5. When the user wants the browser review experience, start the local UI:

   ```powershell
   uv run python -m app.main review
   ```

6. When the user asks to save the drafts to SharePoint, load the Codex workspace
   dependencies and use the returned bundled Python executable to export Word documents:

   ```powershell
   & <workspace-python> -m app.sharepoint_docx_export
   ```

   Add `--run-id <id>` to export only one run. The exporter writes `.docx` files under
   `out/sharepoint-docx`.

   Upload the generated Word files to Ryan's private OneDrive for Business site:

   - Host: `uipath-my.sharepoint.com`
   - Site: `/personal/ryan_sikand_uipath_com`
   - Folder: `Meetings/Tribble Meeting Notes`

   Inspect the destination first, upload only the requested run files with the DOCX MIME
   type, and re-fetch or list the uploaded items to verify both metadata and extracted
   content. Do not create a sharing link or grant permissions unless the user explicitly
   asks.

## Safety

- Treat `tribble.db` as read-only. The adapter uses SQLite URI `mode=ro` plus
  `PRAGMA query_only`.
- Process only meetings with transcript entries.
- Deduplicate using `MeetingRun.tribble_meeting_id`.
- Keep generated transcript files under ignored `data/tribble/`.
- Preserve the app's approval gate: syncing creates draft review runs and never writes
  to Salesforce or Quip automatically. SharePoint upload is performed only when requested.
- Use `--local` unless the user explicitly authorizes sending the selected transcript text
  through the configured OpenAI API.
- Prefer Tribble's existing structured summary and action-item fields in local mode; fall
  back to transcript heuristics only when those fields are absent.
- If the database is not found, report the configured path and explain that
  `TRIBBLE_DB_PATH` can override it.
