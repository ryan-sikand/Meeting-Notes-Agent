from __future__ import annotations

import re
from datetime import date

from app.models import (
    MeetingIntelligence,
    OpportunityAlternative,
    OpportunityMatch,
    SalesforceAccount,
    SalesforceCandidates,
    SalesforceOpportunity,
)

INTERNAL_EMAIL_DOMAINS = {"uipath.com", "salesforce.com"}
HIGH_CONFIDENCE = 70
REVIEW_CONFIDENCE = 40
ACCOUNT_CONFIDENCE = 40
MIN_OPPORTUNITY_MARGIN = 15
MIN_ACCOUNT_MARGIN = 10
MATCH_STOP_WORDS = {
    "and",
    "automation",
    "call",
    "center",
    "command",
    "demo",
    "discovery",
    "meeting",
    "pilot",
    "proposal",
    "renewal",
    "review",
    "service",
    "services",
    "sync",
    "technical",
    "test",
    "the",
    "uipath",
    "with",
    "workshop",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")


def match_opportunity(
    intelligence: MeetingIntelligence,
    candidates: SalesforceCandidates,
    transcript_text: str = "",
    current_user_name: str | None = None,
) -> OpportunityMatch:
    account_rankings = sorted(
        (
            score_account(intelligence, candidates, account, transcript_text)
            for account in candidates.accounts
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    selected_account, account_confidence, account_reasons = select_account(account_rankings)
    account_scores = {account.id: score for score, account, _ in account_rankings}

    alternatives = [
        score_opportunity(
            intelligence,
            candidates,
            opportunity,
            transcript_text,
            current_user_name,
            account_scores,
        )
        for opportunity in candidates.opportunities
    ]
    alternatives.sort(key=lambda item: item.confidence, reverse=True)

    if alternatives:
        best = alternatives[0]
        second_score = alternatives[1].confidence if len(alternatives) > 1 else 0
        unique_best = best.confidence - second_score >= MIN_OPPORTUNITY_MARGIN
        if best.confidence >= HIGH_CONFIDENCE and unique_best:
            return OpportunityMatch(
                opportunity_id=best.opportunity_id,
                opportunity_name=best.opportunity_name,
                account_id=best.account_id,
                account_name=best.account_name,
                confidence=best.confidence,
                account_confidence=account_scores.get(best.account_id or "", 0),
                match_status="matched",
                reasons=best.reasons,
                alternatives=alternatives[1:4],
            )

    if selected_account:
        reasons = [*account_reasons]
        if alternatives and alternatives[0].confidence >= REVIEW_CONFIDENCE:
            reasons.append(
                "Multiple or lower-confidence Opportunity candidates require review; "
                "no Opportunity was assigned automatically."
            )
        else:
            reasons.append("No Opportunity had enough evidence for an automatic match.")
        return OpportunityMatch(
            opportunity_id=None,
            opportunity_name=None,
            account_id=selected_account.id,
            account_name=selected_account.name,
            confidence=account_confidence,
            account_confidence=account_confidence,
            match_status="account_only",
            reasons=reasons,
            alternatives=[
                alternative
                for alternative in alternatives
                if alternative.confidence >= REVIEW_CONFIDENCE
            ][:3],
        )

    if alternatives and alternatives[0].confidence >= REVIEW_CONFIDENCE:
        return OpportunityMatch(
            opportunity_id=None,
            opportunity_name=None,
            account_id=None,
            account_name=None,
            confidence=alternatives[0].confidence,
            account_confidence=0,
            match_status="needs_review",
            reasons=[
                "Salesforce returned a plausible candidate, but the evidence was not "
                "strong or unique enough to assign it automatically."
            ],
            alternatives=alternatives[:3],
        )

    reason = "No sufficiently strong Salesforce Account or Opportunity match was found."
    if candidates.lookup_status == "error":
        reason = f"Salesforce lookup failed: {candidates.lookup_error or 'Unknown error.'}"
    elif candidates.lookup_status == "disabled":
        reason = "Salesforce matching is disabled or not configured."
    elif not candidates.accounts and not candidates.opportunities:
        reason = "No Salesforce candidates were returned for the meeting signals."
    return OpportunityMatch(
        opportunity_id=None,
        opportunity_name=None,
        account_id=None,
        account_name=None,
        confidence=0,
        account_confidence=0,
        match_status="no_match",
        reasons=[reason],
        alternatives=[
            alternative
            for alternative in alternatives
            if alternative.confidence >= REVIEW_CONFIDENCE
        ][:3],
    )


def select_account(
    rankings: list[tuple[int, SalesforceAccount, list[str]]],
) -> tuple[SalesforceAccount | None, int, list[str]]:
    if not rankings:
        return None, 0, []
    best_score, best_account, best_reasons = rankings[0]
    second_score = rankings[1][0] if len(rankings) > 1 else 0
    if best_score < ACCOUNT_CONFIDENCE or best_score - second_score < MIN_ACCOUNT_MARGIN:
        return None, 0, []
    return best_account, best_score, best_reasons


def score_account(
    intelligence: MeetingIntelligence,
    candidates: SalesforceCandidates,
    account: SalesforceAccount,
    transcript_text: str,
) -> tuple[int, SalesforceAccount, list[str]]:
    score = 0
    reasons: list[str] = []
    contact_account_ids = {
        contact.account_id
        for contact in candidates.contacts
        if contact.account_id and attendee_email_matches(contact.email, intelligence)
    }
    if account.id in contact_account_ids:
        score += 60
        reasons.append("An attendee email matched a Salesforce Contact on this Account.")

    account_name = normalize_text(account.name)
    account_tokens = matching_tokens(account.name)
    attendee_name_tokens = {
        token
        for attendee in intelligence.attendees
        for token in matching_tokens(attendee.name or "")
    }
    if (
        len(account_tokens) == 1
        and account_tokens[0] in attendee_name_tokens
        and not intelligence.customer_account_guess
    ):
        return 0, account, ["A one-word Account name matched only an attendee's name."]
    guess = intelligence.customer_account_guess or ""
    guess_tokens = matching_tokens(guess)
    if guess_tokens:
        coverage = token_coverage(account_tokens, guess_tokens)
        if account_name == normalize_text(guess) or coverage >= 0.8:
            score += 50
            reasons.append("The inferred customer name strongly matched this Account.")
        elif coverage >= 0.5:
            score += 30
            reasons.append("The inferred customer name partially matched this Account.")

    local_haystack = normalize_text(
        f"{intelligence.meeting_title} {transcript_text}"
    )
    if len(account_name) >= 4 and account_name in local_haystack:
        score += 45
        reasons.append("The Salesforce Account name was stated in the meeting title or transcript.")
    else:
        title_tokens = matching_tokens(
            f"{intelligence.meeting_title} {intelligence.customer_account_guess or ''}"
        )
        coverage = token_coverage(account_tokens, title_tokens)
        if coverage >= 0.8:
            score += 40
            reasons.append("Distinctive Account-name terms matched the meeting title.")
        elif coverage >= 0.5:
            score += 25
            reasons.append("Some Account-name terms matched the meeting title.")

    acronym = account_acronym(account.name)
    title_token_set = set(matching_tokens(intelligence.meeting_title))
    if len(acronym) >= 3 and acronym in title_token_set:
        score += 40
        reasons.append("The Account acronym matched the meeting title.")

    return min(score, 100), account, reasons


def score_opportunity(
    intelligence: MeetingIntelligence,
    candidates: SalesforceCandidates,
    opportunity: SalesforceOpportunity,
    transcript_text: str,
    current_user_name: str | None,
    account_scores: dict[str, int] | None = None,
) -> OpportunityAlternative:
    score = 0
    reasons: list[str] = []
    account_scores = account_scores or {}
    account_score = account_scores.get(opportunity.account_id or "", 0)
    if account_score >= ACCOUNT_CONFIDENCE:
        score += 30
        reasons.append("The Opportunity belongs to a strongly matched Account.")

    contact_account_ids = {
        contact.account_id
        for contact in candidates.contacts
        if contact.account_id and attendee_email_matches(contact.email, intelligence)
    }
    if opportunity.account_id and opportunity.account_id in contact_account_ids:
        score += 10
        reasons.append("An attendee email matched a Contact on the Opportunity Account.")

    haystack = normalize_text(f"{transcript_text} {intelligence.meeting_title}")
    opportunity_name = normalize_text(opportunity.name)
    opportunity_name_tokens = matching_tokens(opportunity.name)
    if len(opportunity_name_tokens) >= 2 and opportunity_name in haystack:
        score += 55
        reasons.append("The full Opportunity name was mentioned in the title or transcript.")
    else:
        signal_tokens = matching_tokens(
            f"{intelligence.meeting_title} {intelligence.customer_account_guess or ''}"
        )
        opportunity_tokens = opportunity_name_tokens
        overlap = set(signal_tokens) & set(opportunity_tokens)
        coverage = token_coverage(opportunity_tokens, signal_tokens)
        if len(overlap) >= 2 and coverage >= 0.8:
            score += 35
            reasons.append("Distinctive Opportunity-name terms matched the meeting title.")
        elif len(overlap) >= 2 and coverage >= 0.5:
            score += 20
            reasons.append("Some Opportunity-name terms matched the meeting title.")

    signal_numbers = reference_numbers(
        f"{intelligence.meeting_title} {transcript_text}"
    )
    opportunity_numbers = reference_numbers(opportunity.name)
    if signal_numbers & opportunity_numbers:
        score += 40
        reasons.append("A Salesforce reference number matched the meeting content.")

    if opportunity.owner_name and owner_signal(
        opportunity.owner_name,
        intelligence,
        current_user_name,
    ):
        score += 5
        reasons.append("The Opportunity owner matched the current user or an internal attendee.")

    if close_date_relevance(opportunity.close_date):
        score += 5
        reasons.append("The Opportunity close date is relevant to the current quarter.")
    elif close_date_is_stale(opportunity.close_date):
        score -= 15
        reasons.append("The open Opportunity has a past close date, reducing confidence.")

    if not reasons:
        reasons.append("The Opportunity was returned, but no strong deterministic signal matched.")

    return OpportunityAlternative(
        opportunity_id=opportunity.id,
        opportunity_name=opportunity.name,
        account_id=opportunity.account_id,
        account_name=opportunity.account_name,
        confidence=max(0, min(score, 100)),
        reasons=reasons,
    )


def attendee_email_matches(contact_email: str | None, intelligence: MeetingIntelligence) -> bool:
    if not contact_email:
        return False
    normalized = contact_email.lower()
    return any(
        attendee.email and attendee.email.lower() == normalized
        for attendee in intelligence.attendees
    )


def owner_signal(
    owner_name: str,
    intelligence: MeetingIntelligence,
    current_user_name: str | None,
) -> bool:
    owner = owner_name.lower()
    if current_user_name and owner == current_user_name.lower():
        return True
    for attendee in intelligence.attendees:
        email = attendee.email or ""
        domain = email.split("@")[-1].lower() if "@" in email else ""
        if domain in INTERNAL_EMAIL_DOMAINS and attendee.name and attendee.name.lower() in owner:
            return True
    return False


def close_date_relevance(close_date: str | None) -> bool:
    parsed = parse_close_date(close_date)
    if not parsed:
        return False
    return abs((parsed - date.today()).days) <= 120


def close_date_is_stale(close_date: str | None) -> bool:
    parsed = parse_close_date(close_date)
    return bool(parsed and (date.today() - parsed).days > 30)


def parse_close_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def normalize_text(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower()))


def matching_tokens(value: str) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(value.lower())
        if len(token) >= 2 and token not in MATCH_STOP_WORDS
    ]


def token_coverage(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / min(len(left_set), len(right_set))


def account_acronym(value: str) -> str:
    tokens = [
        token
        for token in TOKEN_RE.findall(value.lower())
        if token not in MATCH_STOP_WORDS and len(token) > 1
    ]
    return "".join(token[0] for token in tokens)


def reference_numbers(value: str) -> set[str]:
    return set(re.findall(r"\b\d{6,}\b", value))
