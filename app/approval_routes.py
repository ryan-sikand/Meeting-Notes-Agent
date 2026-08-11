from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import RunStatus
from app.openai_client import OpenAIClientError
from app.persistence import RunStore
from app.quip_client import QuipClient
from app.salesforce_client import SalesforceClient
from app.zoom_docs_client import ZoomDocsClient

router = APIRouter()
templates = Jinja2Templates(directory="templates")
TranscriptUpload = Annotated[UploadFile, File(...)]
ThreadIdForm = Annotated[str | None, Form()]
PastedTranscript = Annotated[str, Form()]
PastedTitle = Annotated[str | None, Form()]


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"runs": RunStore().list()},
    )


@router.get("/zoom-doc-result", response_class=HTMLResponse)
def zoom_doc_result(request: Request, url: str = Query(...)) -> HTMLResponse:
    try:
        inspection = ZoomDocsClient().inspect(url)
    except Exception as exc:
        inspection = {"url": url, "error": str(exc), "has_embedded_content": False}
    return templates.TemplateResponse(
        request,
        "zoom_doc_result.html",
        {"url": url, "inspection": inspection},
    )


@router.post("/process-upload")
async def process_upload(request: Request, transcript: TranscriptUpload) -> RedirectResponse:
    from app.main import process_transcript

    settings = get_settings()
    upload_dir = Path(settings.data_dir).parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / Path(transcript.filename or "transcript.txt").name
    target.write_bytes(await transcript.read())
    try:
        run = process_transcript(target)
    except OpenAIClientError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": "OpenAI Request Failed", "message": str(exc)},
            status_code=502,
        )
    return RedirectResponse(url=f"/review/{run.run_id}", status_code=303)


@router.post("/process-paste")
def process_paste(
    request: Request,
    transcript_text: PastedTranscript,
    title: PastedTitle = None,
) -> RedirectResponse:
    from app.main import process_transcript

    text = transcript_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Transcript text is required.")

    settings = get_settings()
    pasted_dir = Path(settings.data_dir).parent / "pasted"
    pasted_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_paste_filename(title or "pasted-transcript")
    target = next_available_path(pasted_dir / f"{filename}.txt")
    target.write_text(text, encoding="utf-8")
    try:
        run = process_transcript(target)
    except OpenAIClientError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": "OpenAI Request Failed", "message": str(exc)},
            status_code=502,
        )
    return RedirectResponse(url=f"/review/{run.run_id}", status_code=303)


@router.get("/review/{run_id}", response_class=HTMLResponse)
def review(request: Request, run_id: str) -> HTMLResponse:
    try:
        run = RunStore().get(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(request, "review.html", {"run": run})


@router.post("/review/{run_id}/approve-quip", response_class=HTMLResponse)
def approve_quip(
    request: Request,
    run_id: str,
    thread_id: ThreadIdForm = None,
) -> HTMLResponse:
    run = load_run(run_id)
    quip = QuipClient()
    if thread_id:
        run.quip_result = quip.append_to_document(thread_id, run.quip_note.html_content)
    else:
        run.quip_result = quip.create_document(run.quip_note.title, run.quip_note.html_content)
    run.status = RunStatus.approved_quip_only
    RunStore().save(run)
    return templates.TemplateResponse(request, "result.html", {"run": run})


@router.post("/review/{run_id}/approve-quip-salesforce-task", response_class=HTMLResponse)
def approve_quip_salesforce_task(request: Request, run_id: str) -> HTMLResponse:
    run = load_run(run_id)
    quip = QuipClient()
    run.quip_result = quip.create_document(run.quip_note.title, run.quip_note.html_content)

    if run.salesforce_match.opportunity_id:
        task = run.proposed_salesforce_updates.create_task or {}
        run.salesforce_result = SalesforceClient().create_task_for_opportunity(
            opportunity_id=run.salesforce_match.opportunity_id,
            subject=str(task.get("Subject", f"Meeting notes: {run.intelligence.meeting_title}")),
            description=str(task.get("Description", run.intelligence.executive_summary)),
            approved=True,
        )
    else:
        run.salesforce_result = {"skipped": True, "reason": "No matched Opportunity ID."}

    run.status = RunStatus.approved_quip_salesforce_task
    RunStore().save(run)
    return templates.TemplateResponse(request, "result.html", {"run": run})


@router.post("/review/{run_id}/reject", response_class=HTMLResponse)
def reject(request: Request, run_id: str) -> HTMLResponse:
    run = RunStore().update_status(run_id, RunStatus.rejected)
    return templates.TemplateResponse(request, "result.html", {"run": run})


def load_run(run_id: str):
    try:
        return RunStore().get(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def safe_paste_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value)
    return safe.strip("-") or "pasted-transcript"


def next_available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_stem(f"{path.stem}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate pasted transcript filename.")
