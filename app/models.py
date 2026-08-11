from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MeetingMetadata(BaseModel):
    title: str | None = None
    meeting_date: str | None = None
    source_filename: str | None = None


class TranscriptSegment(BaseModel):
    speaker: str | None = None
    timestamp: str | None = None
    text: str


class ParsedTranscript(BaseModel):
    source_path: str
    filename: str
    raw_text: str
    normalized_text: str
    segments: list[TranscriptSegment]
    metadata: MeetingMetadata = Field(default_factory=MeetingMetadata)


class Attendee(BaseModel):
    name: str | None = None
    email: str | None = None
    company: str | None = None
    role: str | None = None


class ActionItem(BaseModel):
    owner: str | None = None
    task: str
    due_date: str | None = None


class MeetingIntelligence(BaseModel):
    meeting_title: str
    meeting_date: str | None = None
    customer_account_guess: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    executive_summary: str
    key_points: list[str] = Field(default_factory=list)
    customer_pain_points: list[str] = Field(default_factory=list)
    business_outcomes: list[str] = Field(default_factory=list)
    technical_requirements: list[str] = Field(default_factory=list)
    competitors_mentioned: list[str] = Field(default_factory=list)
    risks_or_blockers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    timeline_signals: list[str] = Field(default_factory=list)
    budget_signals: list[str] = Field(default_factory=list)
    proposed_next_step: str
    follow_up_email_draft: str | None = None


class SalesforceContact(BaseModel):
    id: str = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")
    email: str | None = Field(default=None, alias="Email")
    account_id: str | None = Field(default=None, alias="AccountId")
    account_name: str | None = None

    model_config = {"populate_by_name": True}


class SalesforceAccount(BaseModel):
    id: str = Field(alias="Id")
    name: str = Field(alias="Name")

    model_config = {"populate_by_name": True}


class SalesforceOpportunity(BaseModel):
    id: str = Field(alias="Id")
    name: str = Field(alias="Name")
    account_id: str | None = Field(default=None, alias="AccountId")
    account_name: str | None = None
    stage_name: str | None = Field(default=None, alias="StageName")
    close_date: str | None = Field(default=None, alias="CloseDate")
    amount: float | None = Field(default=None, alias="Amount")
    owner_name: str | None = None
    next_step: str | None = Field(default=None, alias="NextStep")
    last_modified_date: str | None = Field(default=None, alias="LastModifiedDate")

    model_config = {"populate_by_name": True}


class SalesforceCandidates(BaseModel):
    contacts: list[SalesforceContact] = Field(default_factory=list)
    accounts: list[SalesforceAccount] = Field(default_factory=list)
    opportunities: list[SalesforceOpportunity] = Field(default_factory=list)
    lookup_status: str = "success"
    lookup_error: str | None = None


class OpportunityAlternative(BaseModel):
    opportunity_id: str | None
    opportunity_name: str | None
    account_id: str | None
    account_name: str | None
    confidence: int
    reasons: list[str]


class OpportunityMatch(OpportunityAlternative):
    match_status: str = "no_match"
    account_confidence: int = 0
    alternatives: list[OpportunityAlternative] = Field(default_factory=list)


class ProposedSalesforceUpdates(BaseModel):
    create_task: dict[str, Any] | None = None
    opportunity_updates: dict[str, Any] = Field(default_factory=dict)


class GeneratedNote(BaseModel):
    title: str
    html_content: str


class RunStatus(StrEnum):
    pending = "pending"
    approved_quip_only = "approved_quip_only"
    approved_quip_salesforce_task = "approved_quip_salesforce_task"
    rejected = "rejected"


class MeetingRun(BaseModel):
    run_id: str
    created_at: datetime
    status: RunStatus = RunStatus.pending
    transcript_path: str
    transcript_filename: str
    zoom_meeting_uuid: str | None = None
    tribble_meeting_id: str | None = None
    intelligence: MeetingIntelligence
    salesforce_match: OpportunityMatch
    proposed_salesforce_updates: ProposedSalesforceUpdates
    quip_note: GeneratedNote
    quip_result: dict[str, Any] | None = None
    salesforce_result: dict[str, Any] | None = None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def safe_filename(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in ("-", "_", ".") else "-" for char in value
    )


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()
