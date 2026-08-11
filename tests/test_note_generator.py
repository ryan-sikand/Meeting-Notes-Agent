from app.models import MeetingIntelligence, OpportunityMatch
from app.note_generator import generate_quip_note


def test_generate_quip_note_contains_required_sections() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="Discovery",
        meeting_date="2026-06-09",
        customer_account_guess="Acme Corp",
        executive_summary="Acme wants a faster renewal-risk workflow.",
        customer_pain_points=["Manual renewal tracking"],
        business_outcomes=["Reduce churn risk"],
        technical_requirements=["Salesforce integration"],
        decisions=["Proceed to workshop"],
        risks_or_blockers=["Security review"],
        competitors_mentioned=["CompetitorCo"],
        proposed_next_step="Schedule a technical workshop.",
        follow_up_email_draft="Subject: Follow-up\n\nThanks everyone.",
    )
    match = OpportunityMatch(
        opportunity_id="0061",
        opportunity_name="Acme Expansion",
        account_id="0011",
        account_name="Acme Corp",
        confidence=90,
        match_status="matched",
        reasons=["Account matched."],
    )

    note = generate_quip_note(intelligence, match)

    assert note.title == "2026-06-09 - Acme Corp - Discovery"
    for heading in [
        "Executive Summary",
        "Attendees",
        "Key Points",
        "Customer Pain Points",
        "Business Outcomes",
        "Technical Requirements",
        "Decisions",
        "Risks / Blockers",
        "Competitors Mentioned",
        "Action Items",
        "Recommended Next Step",
        "Follow-Up Email Draft",
        "Salesforce Match",
    ]:
        assert f"<h2>{heading}</h2>" in note.html_content
    assert "Status: Matched" in note.html_content
