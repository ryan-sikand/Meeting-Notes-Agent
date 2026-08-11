from datetime import date
from pathlib import Path

from app.models import (
    Attendee,
    MeetingIntelligence,
    SalesforceAccount,
    SalesforceCandidates,
    SalesforceOpportunity,
)
from app.opportunity_matcher import match_opportunity


def test_match_opportunity_scores_deterministic_signals() -> None:
    candidates = SalesforceCandidates.model_validate_json(
        Path("tests/fixtures/sample_salesforce_opps.json").read_text(encoding="utf-8")
    )
    intelligence = MeetingIntelligence(
        meeting_title="Acme Discovery",
        meeting_date="2026-06-09",
        customer_account_guess="Acme Corp",
        attendees=[Attendee(name="Sarah Connor", email="sarah@acme.example", company="Acme")],
        executive_summary="Acme needs automation.",
        proposed_next_step="Schedule technical workshop.",
    )

    match = match_opportunity(
        intelligence,
        candidates,
        transcript_text="Discussed the Acme Expansion opportunity.",
        current_user_name="Ryan Sikand",
    )

    assert match.opportunity_id == "0061"
    assert match.account_id == "0011"
    assert match.confidence == 100
    assert match.match_status == "matched"
    assert len(match.reasons) >= 4


def test_match_opportunity_handles_no_candidates() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="Unknown Meeting",
        executive_summary="No CRM data.",
        proposed_next_step="Follow up.",
    )

    match = match_opportunity(intelligence, SalesforceCandidates())

    assert match.opportunity_id is None
    assert match.confidence == 0
    assert match.match_status == "no_match"


def test_match_opportunity_keeps_ambiguous_renewals_at_account_only() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="Navy FMS Meeting",
        executive_summary="Discussed the Navy FMS program.",
        proposed_next_step="Confirm the relevant renewal.",
    )
    account = SalesforceAccount(
        Id="001-navy",
        Name="Navy Financial Management Systems (FMS)",
    )
    candidates = SalesforceCandidates(
        accounts=[account],
        opportunities=[
            SalesforceOpportunity(
                Id="006-a",
                Name="Navy Financial Management Systems (FMS) Renewal 00144047 2027",
                AccountId=account.id,
                account_name=account.name,
                CloseDate="2027-03-26",
            ),
            SalesforceOpportunity(
                Id="006-b",
                Name="Navy Financial Management Systems (FMS) Renewal 00146264 2027",
                AccountId=account.id,
                account_name=account.name,
                CloseDate="2027-05-14",
            ),
        ],
    )

    match = match_opportunity(intelligence, candidates)

    assert match.match_status == "account_only"
    assert match.account_id == account.id
    assert match.opportunity_id is None
    assert len(match.alternatives) == 2


def test_match_opportunity_ignores_generic_one_word_opportunity_name() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="NIWC PAC Command Center Pilot",
        executive_summary="The team discussed testing the pilot.",
        proposed_next_step="Confirm next steps.",
    )
    candidates = SalesforceCandidates(
        opportunities=[
            SalesforceOpportunity(
                Id="006-test",
                Name="test",
                AccountId="001-ibm",
                account_name="IBM Corporation HQ",
                CloseDate="2026-09-30",
            )
        ]
    )

    match = match_opportunity(
        intelligence,
        candidates,
        transcript_text="We need to test the command center.",
    )

    assert match.match_status == "no_match"
    assert match.opportunity_id is None


def test_match_opportunity_flags_plausible_nonexact_candidate_for_review() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="NIWC PAC Command Center Pilot",
        executive_summary="The team discussed the pilot.",
        proposed_next_step="Confirm scope.",
    )
    candidates = SalesforceCandidates(
        opportunities=[
            SalesforceOpportunity(
                Id="006-niwc",
                Name="NIWC PAC Document Understanding Pilot",
                AccountId="001-navwar",
                account_name="Navy NAVWAR",
                CloseDate=date.today().isoformat(),
            )
        ]
    )

    match = match_opportunity(intelligence, candidates)

    assert match.match_status == "needs_review"
    assert match.opportunity_id is None
    assert match.alternatives[0].opportunity_id == "006-niwc"


def test_match_opportunity_rejects_account_that_is_only_an_attendee_name() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="Ryan & Pradeep Weekly Sync",
        attendees=[Attendee(name="Ryan Sikand"), Attendee(name="Pradeep Paruchuri")],
        executive_summary="Internal sync.",
        proposed_next_step="Follow up.",
    )
    candidates = SalesforceCandidates(
        accounts=[SalesforceAccount(Id="001-ryan", Name="Ryan")]
    )

    match = match_opportunity(intelligence, candidates)

    assert match.match_status == "no_match"
    assert match.account_id is None
