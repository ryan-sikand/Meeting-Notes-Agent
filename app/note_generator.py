from datetime import date
from html import escape

from app.models import GeneratedNote, MeetingIntelligence, OpportunityMatch, parse_date


def generate_quip_note(
    intelligence: MeetingIntelligence,
    match: OpportunityMatch,
) -> GeneratedNote:
    title = build_note_title(intelligence, match)
    sections = [
        ("Executive Summary", paragraph(intelligence.executive_summary)),
        (
            "Attendees",
            unordered_list([attendee.name or "Unknown" for attendee in intelligence.attendees]),
        ),
        ("Key Points", unordered_list(intelligence.key_points)),
        ("Customer Pain Points", unordered_list(intelligence.customer_pain_points)),
        ("Business Outcomes", unordered_list(intelligence.business_outcomes)),
        ("Technical Requirements", unordered_list(intelligence.technical_requirements)),
        ("Decisions", unordered_list(intelligence.decisions)),
        ("Risks / Blockers", unordered_list(intelligence.risks_or_blockers)),
        ("Open Questions", unordered_list(intelligence.open_questions)),
        ("Competitors Mentioned", unordered_list(intelligence.competitors_mentioned)),
        ("Action Items", action_items(intelligence)),
        ("Recommended Next Step", paragraph(intelligence.proposed_next_step)),
        ("Follow-Up Email Draft", paragraph(intelligence.follow_up_email_draft)),
        ("Salesforce Match", salesforce_match(match)),
    ]
    body = "\n".join(f"<h2>{escape(heading)}</h2>\n{content}" for heading, content in sections)
    return GeneratedNote(title=title, html_content=f"<h1>{escape(title)}</h1>\n{body}\n")


def build_note_title(intelligence: MeetingIntelligence, match: OpportunityMatch) -> str:
    meeting_date = parse_date(intelligence.meeting_date) or date.today()
    account_name = match.account_name or intelligence.customer_account_guess or "Unknown Account"
    return f"{meeting_date.isoformat()} - {account_name} - {intelligence.meeting_title}"


def paragraph(value: str | None) -> str:
    escaped = escape(value or "None captured.").replace("\n", "<br>")
    return f"<p>{escaped}</p>"


def unordered_list(values: list[str]) -> str:
    if not values:
        return "<p>None captured.</p>"
    return "<ul>" + "".join(f"<li>{escape(value)}</li>" for value in values) + "</ul>"


def action_items(intelligence: MeetingIntelligence) -> str:
    if not intelligence.action_items:
        return "<p>None captured.</p>"
    items = []
    for item in intelligence.action_items:
        owner = f" ({escape(item.owner)})" if item.owner else ""
        due = f" due {escape(item.due_date)}" if item.due_date else ""
        items.append(f"<li>{escape(item.task)}{owner}{due}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def salesforce_match(match: OpportunityMatch) -> str:
    lines = [
        f"Status: {match.match_status.replace('_', ' ').title()}",
        f"Account: {match.account_name or 'No match'}",
        f"Opportunity: {match.opportunity_name or 'No match'}",
        f"Confidence: {match.confidence}",
    ]
    reasons = [f"Reason: {reason}" for reason in match.reasons]
    alternatives = [
        "Candidate: "
        f"{alternative.account_name or 'Unknown Account'} / "
        f"{alternative.opportunity_name or 'Unknown Opportunity'} "
        f"({alternative.confidence})"
        for alternative in match.alternatives[:3]
    ]
    return unordered_list(lines + reasons + alternatives)
