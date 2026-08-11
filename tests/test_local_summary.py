from typing import Any

import httpx

from app.config import Settings
from app.models import MeetingMetadata
from app.openai_client import OpenAIClient, local_transcript_summary


def test_local_transcript_summary_extracts_structured_recap() -> None:
    transcript = """
Sarah: We agreed to proceed with the security workshop.
Ryan: I will send the architecture notes by Friday.
Pablo: The main blocker is security review timing.
Sarah: Can you schedule the follow-up?
Ryan: What budget range should we use?
"""

    summary = local_transcript_summary(
        transcript,
        MeetingMetadata(title="Pablo & Ryan Sync", meeting_date="2026-06-10"),
    )

    assert summary.meeting_title == "Pablo & Ryan Sync"
    assert summary.attendees[0].name == "Sarah"
    assert "security workshop" in summary.decisions[0]
    assert summary.action_items[0].owner == "Ryan"
    assert summary.action_items[0].due_date == "Friday"
    assert "security review" in summary.risks_or_blockers[0]
    assert "Subject: Follow-up from Pablo & Ryan Sync" in (summary.follow_up_email_draft or "")


def test_local_transcript_summary_filters_zoom_chatter_and_extracts_followups() -> None:
    transcript = """
[10:01:35] Speaker 4: No relation, must be a copycat.
[10:03:28] Speaker 1: One of the challenges was around data aggregation. We pull data
from systems already in use and consolidate it on a dashboard or COP. We're not trying
to create a new system of record.
[10:14:03] Speaker 3: Are there any known data type limitations currently?
[10:16:15] Speaker 4: As far as the ATOs you've got within DOW, are any of them up on
the classified side?
[10:16:28] Speaker 1: The IC customer uses this on JWICS. I'd have to get to another
colleague, but I can get further information.
[10:18:11] Speaker 1: If you could share the challenge of what you're trying to do,
that'd be helpful.
[10:20:04] Speaker 3: We're trying to rectify those 97 requirements for ZT as well.
[10:30:20] Speaker 3: Right now it's not a priority because we have too many projects.
Definitely reach out when we have a more open schedule for the demo.
"""

    summary = local_transcript_summary(
        transcript,
        MeetingMetadata(title="pasted transcript 6", meeting_date="2026-06-10"),
    )

    assert summary.customer_account_guess is None
    assert "copycat" not in summary.executive_summary.lower()
    assert "data aggregation" in summary.executive_summary.lower()
    assert "Zero Trust requirement alignment still needs to be clarified." in (
        summary.risks_or_blockers
    )
    assert "Customer indicated this is not an immediate priority due to other projects." in (
        summary.risks_or_blockers
    )
    assert [item.task for item in summary.action_items] == [
        "Follow up internally on classified/JWICS deployment and ATO details.",
        "Share more detail on the data aggregation challenge, source systems, and desired output.",
        "Reach out when the team has schedule availability for a demo.",
    ]
    assert "\\n" not in (summary.follow_up_email_draft or "")
    assert "\n\nAction items:\n" in (summary.follow_up_email_draft or "")


class FailingHttpClient:
    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> "FailingHttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, *_: object, **__: object) -> httpx.Response:
        raise httpx.ConnectError("offline")


def test_openai_connection_error_falls_back_to_local(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.openai_client.httpx.Client", FailingHttpClient)
    settings = Settings(
        OPENAI_API_KEY="configured-but-offline",
        OPENAI_FALLBACK_TO_LOCAL=True,
    )

    summary = OpenAIClient(settings).summarize_meeting(
        "Ryan: We agreed to schedule the workshop.",
        MeetingMetadata(title="Offline Meeting", meeting_date="2026-07-27"),
    )

    assert summary.meeting_title == "Offline Meeting"
    assert summary.decisions == ["We agreed to schedule the workshop."]
