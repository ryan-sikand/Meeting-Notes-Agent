import pytest

from app.config import Settings
from app.models import Attendee, MeetingIntelligence
from app.salesforce_client import SalesforceClient, candidate_search_terms, name_match_clause


def salesforce_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "SALESFORCE_CLIENT_ID": "client-id",
        "SALESFORCE_CLIENT_SECRET": "client-secret",
        "SALESFORCE_USERNAME": "ryan@example.com",
        "SALESFORCE_PASSWORD": "password",
        "SALESFORCE_SECURITY_TOKEN": "token",
        "DRY_RUN": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_auth_check_reports_unconfigured_without_authenticating(monkeypatch) -> None:
    client = SalesforceClient(Settings(_env_file=None, SALESFORCE_CLI_ENABLED=False))
    monkeypatch.setattr(
        client,
        "authenticate",
        lambda: (_ for _ in ()).throw(AssertionError("authenticate should not be called")),
    )

    result = client.auth_check()

    assert result == {
        "configured": False,
        "authenticated": False,
        "dry_run": True,
        "message": "Salesforce credentials are not configured.",
    }


def test_auth_check_confirms_query_access(monkeypatch) -> None:
    client = SalesforceClient(salesforce_settings())

    def authenticate() -> None:
        client.instance_url = "https://example.my.salesforce.com"
        client.access_token = "access-token"

    monkeypatch.setattr(client, "authenticate", authenticate)
    monkeypatch.setattr(client, "query", lambda _soql: [{"Id": "0051", "Name": "Ryan"}])

    result = client.auth_check()

    assert result["configured"] is True
    assert result["authenticated"] is True
    assert result["query_access"] is True
    assert result["user_name"] == "Ryan"
    assert result["dry_run"] is True


def test_auth_check_uses_existing_salesforce_cli_login(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        SALESFORCE_CLI_ENABLED=True,
        SALESFORCE_CLI_ALIAS="uipath",
    )
    client = SalesforceClient(settings)
    monkeypatch.setattr(client, "_cli_path", lambda: "sf")
    monkeypatch.setattr(
        client,
        "_run_sf_json",
        lambda _arguments: {
            "accessToken": "access-token",
            "instanceUrl": "https://example.my.salesforce.com",
            "username": "ryan@example.com",
        },
    )
    monkeypatch.setattr(client, "query", lambda _soql: [{"Id": "0051", "Name": "Ryan"}])

    result = client.auth_check()

    assert result["authenticated"] is True
    assert result["connection_mode"] == "salesforce_cli"
    assert result["cli_alias"] == "uipath"
    assert result["read_only"] is True


def test_cli_connection_rejects_salesforce_writes(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        SALESFORCE_CLI_ENABLED=True,
        SALESFORCE_CLI_ALIAS="uipath",
        DRY_RUN=False,
    )
    client = SalesforceClient(settings)
    monkeypatch.setattr(client, "_cli_path", lambda: "sf")

    with pytest.raises(PermissionError, match="read-only"):
        client.create_task_for_opportunity("0061", "Meeting", "Notes", approved=True)


def test_candidate_search_terms_keep_customer_acronyms_and_drop_meeting_words() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="NIWC PAC Command Center Pilot",
        executive_summary="Pilot discussion.",
        proposed_next_step="Review next steps.",
    )

    terms = candidate_search_terms(intelligence)

    assert "NIWC PAC" in terms
    assert "NIWC" in terms
    assert "PAC" not in terms
    assert "Pilot" not in terms


def test_candidate_lookup_failure_does_not_block_note_creation(monkeypatch) -> None:
    client = SalesforceClient(salesforce_settings())
    intelligence = MeetingIntelligence(
        meeting_title="Customer meeting",
        executive_summary="Discussion.",
        proposed_next_step="Follow up.",
    )
    monkeypatch.setattr(
        client,
        "authenticate",
        lambda: (_ for _ in ()).throw(RuntimeError("session expired")),
    )

    candidates = client.find_candidates(intelligence)

    assert candidates.lookup_status == "error"
    assert candidates.lookup_error == "session expired"


def test_name_match_clause_requires_every_word_in_a_phrase() -> None:
    clause = name_match_clause("Navy FMS")

    assert clause == "(Name LIKE '%Navy%' AND Name LIKE '%FMS%')"


def test_candidate_search_terms_skip_people_only_weekly_sync() -> None:
    intelligence = MeetingIntelligence(
        meeting_title="Ryan & Pradeep Weekly Sync",
        attendees=[Attendee(name="Ryan Sikand"), Attendee(name="Pradeep Paruchuri")],
        executive_summary="Internal sync.",
        proposed_next_step="Follow up.",
    )

    assert candidate_search_terms(intelligence) == []
