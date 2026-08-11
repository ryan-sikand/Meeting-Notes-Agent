import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import (
    MeetingIntelligence,
    ProposedSalesforceUpdates,
    SalesforceAccount,
    SalesforceCandidates,
    SalesforceContact,
    SalesforceOpportunity,
)

LOGGER = logging.getLogger(__name__)

INTERNAL_EMAIL_DOMAINS = {"uipath.com", "salesforce.com"}
TITLE_STOP_WORDS = {
    "and",
    "app",
    "biweekly",
    "bom",
    "brief",
    "call",
    "case",
    "center",
    "command",
    "demo",
    "discovery",
    "discussion",
    "internal",
    "kickoff",
    "meeting",
    "monthly",
    "pilot",
    "poc",
    "pre",
    "prep",
    "read",
    "review",
    "sync",
    "the",
    "transcript",
    "uipath",
    "use",
    "weekly",
    "with",
    "workshop",
    "chat",
    "new",
    "saved",
}
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+'/-]*")


class SalesforceClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.instance_url: str | None = None
        self.access_token: str | None = None
        self.authenticated_username: str | None = None
        self.cli_authenticated = False

    def find_candidates(self, intelligence: MeetingIntelligence) -> SalesforceCandidates:
        if not self.configured:
            LOGGER.warning(
                "Salesforce is not configured; returning empty matching candidates."
            )
            return SalesforceCandidates(lookup_status="disabled")

        try:
            self.authenticate()
            contacts = self.query_contacts(intelligence)
            accounts = self.query_accounts(intelligence, contacts)
            named_opportunities = self.query_named_opportunities(intelligence)

            accounts_by_id = {account.id: account for account in accounts}
            for opportunity in named_opportunities:
                if opportunity.account_id and opportunity.account_name:
                    accounts_by_id.setdefault(
                        opportunity.account_id,
                        SalesforceAccount(
                            Id=opportunity.account_id,
                            Name=opportunity.account_name,
                        ),
                    )

            matched_accounts = list(accounts_by_id.values())
            account_opportunities = self.query_open_opportunities(matched_accounts)
            opportunities_by_id = {
                opportunity.id: opportunity
                for opportunity in [*named_opportunities, *account_opportunities]
            }
            return SalesforceCandidates(
                contacts=contacts,
                accounts=matched_accounts,
                opportunities=list(opportunities_by_id.values()),
            )
        except (httpx.HTTPError, RuntimeError, subprocess.SubprocessError) as exc:
            message = " ".join(str(exc).split())[:240] or type(exc).__name__
            LOGGER.warning("Salesforce candidate lookup failed: %s", message)
            return SalesforceCandidates(lookup_status="error", lookup_error=message)

    @property
    def password_flow_configured(self) -> bool:
        required = [
            self.settings.salesforce_client_id,
            self.settings.salesforce_client_secret,
            self.settings.salesforce_username,
            self.settings.salesforce_password,
        ]
        return all(required)

    @property
    def cli_configured(self) -> bool:
        return self.settings.salesforce_cli_enabled and self._cli_path() is not None

    @property
    def configured(self) -> bool:
        return self.password_flow_configured or self.cli_configured

    @property
    def connection_mode(self) -> str | None:
        if self.password_flow_configured:
            return "password_flow"
        if self.cli_configured:
            return "salesforce_cli"
        return None

    def auth_check(self) -> dict[str, Any]:
        if not self.configured:
            message = "Salesforce credentials are not configured."
            if self.settings.salesforce_cli_enabled:
                message = "Salesforce CLI matching is enabled, but the sf command was not found."
            return {
                "configured": False,
                "authenticated": False,
                "dry_run": self.settings.dry_run,
                "message": message,
            }

        self.authenticate()
        username = self.authenticated_username or self.settings.salesforce_username
        records = []
        if username:
            records = self.query(
                "SELECT Id, Name FROM User WHERE Username = "
                f"{quote_soql(username)} LIMIT 1"
            )
        return {
            "configured": True,
            "authenticated": True,
            "query_access": bool(records),
            "user_name": records[0].get("Name") if records else None,
            "username": username,
            "instance_url": self.instance_url,
            "api_version": self.settings.salesforce_api_version,
            "connection_mode": self.connection_mode,
            "cli_alias": (
                self.settings.salesforce_cli_alias
                if self.connection_mode == "salesforce_cli"
                else None
            ),
            "read_only": self.connection_mode == "salesforce_cli",
            "dry_run": self.settings.dry_run,
        }

    def authenticate(self) -> None:
        if self.instance_url and (self.access_token or self.cli_authenticated):
            return
        if self.password_flow_configured:
            self._authenticate_password_flow()
            return
        if self.cli_configured:
            self._authenticate_salesforce_cli()
            return
        raise RuntimeError("Salesforce is not configured.")

    def _authenticate_password_flow(self) -> None:
        password = (self.settings.salesforce_password or "") + (
            self.settings.salesforce_security_token or ""
        )
        payload = {
            "grant_type": "password",
            "client_id": self.settings.salesforce_client_id,
            "client_secret": self.settings.salesforce_client_secret,
            "username": self.settings.salesforce_username,
            "password": password,
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.settings.salesforce_login_url}/services/oauth2/token",
                data=payload,
            )
            response.raise_for_status()
        body = response.json()
        self.access_token = body["access_token"]
        self.instance_url = body["instance_url"]
        self.authenticated_username = self.settings.salesforce_username

    def _authenticate_salesforce_cli(self) -> None:
        result = self._run_sf_json(
            [
                "org",
                "display",
                "--verbose",
                "--target-org",
                self.settings.salesforce_cli_alias,
            ]
        )
        instance_url = result.get("instanceUrl")
        if not instance_url:
            raise RuntimeError("Salesforce CLI did not return an instance URL.")
        self.instance_url = str(instance_url)
        self.authenticated_username = str(result.get("username") or "") or None
        self.cli_authenticated = True

    def query_contacts(self, intelligence: MeetingIntelligence) -> list[SalesforceContact]:
        emails = sorted(
            {
                attendee.email.lower()
                for attendee in intelligence.attendees
                if attendee.email and not is_internal_email(attendee.email)
            }
        )
        domains = sorted({email.split("@", 1)[1] for email in emails if "@" in email})
        clauses = []
        if emails:
            clauses.append(f"Email IN ({','.join(quote_soql(email) for email in emails)})")
        if domains:
            domain_clauses = [f"Email LIKE {quote_soql('%@' + domain)}" for domain in domains]
            clauses.append("(" + " OR ".join(domain_clauses) + ")")
        if not clauses:
            return []

        soql = (
            "SELECT Id, Name, Email, AccountId, Account.Name FROM Contact WHERE "
            + " OR ".join(clauses)
            + " LIMIT 50"
        )
        records = self.query(soql)
        return [
            SalesforceContact(
                Id=record["Id"],
                Name=record.get("Name"),
                Email=record.get("Email"),
                AccountId=record.get("AccountId"),
                account_name=(record.get("Account") or {}).get("Name"),
            )
            for record in records
        ]

    def query_accounts(
        self,
        intelligence: MeetingIntelligence,
        contacts: list[SalesforceContact],
    ) -> list[SalesforceAccount]:
        account_ids = sorted({contact.account_id for contact in contacts if contact.account_id})
        clauses = []
        if account_ids:
            quoted_ids = ",".join(quote_soql(account_id) for account_id in account_ids)
            clauses.append(f"Id IN ({quoted_ids})")
        for term in candidate_search_terms(intelligence):
            clauses.append(name_match_clause(term))
        if not clauses:
            return []

        soql = (
            "SELECT Id, Name FROM Account WHERE "
            + " OR ".join(clauses)
            + " ORDER BY LastModifiedDate DESC LIMIT 50"
        )
        return [
            SalesforceAccount(Id=record["Id"], Name=record["Name"])
            for record in self.query(soql)
        ]

    def query_named_opportunities(
        self,
        intelligence: MeetingIntelligence,
    ) -> list[SalesforceOpportunity]:
        terms = candidate_search_terms(intelligence)
        if not terms:
            return []
        clauses = [name_match_clause(term) for term in terms]
        soql = (
            "SELECT Id, Name, AccountId, Account.Name, StageName, CloseDate, Amount, "
            "Owner.Name, NextStep, LastModifiedDate FROM Opportunity "
            "WHERE IsClosed = false AND ("
            + " OR ".join(clauses)
            + ") ORDER BY LastModifiedDate DESC LIMIT 50"
        )
        return [self._opportunity_from_record(record) for record in self.query(soql)]

    def query_open_opportunities(
        self,
        accounts: list[SalesforceAccount],
    ) -> list[SalesforceOpportunity]:
        if not accounts:
            return []
        account_ids = ",".join(quote_soql(account.id) for account in accounts)
        soql = (
            "SELECT Id, Name, AccountId, Account.Name, StageName, CloseDate, Amount, "
            "Owner.Name, NextStep, LastModifiedDate FROM Opportunity "
            f"WHERE IsClosed = false AND AccountId IN ({account_ids}) "
            "ORDER BY LastModifiedDate DESC LIMIT 100"
        )
        return [self._opportunity_from_record(record) for record in self.query(soql)]

    @staticmethod
    def _opportunity_from_record(record: dict[str, Any]) -> SalesforceOpportunity:
        return SalesforceOpportunity(
            Id=record["Id"],
            Name=record["Name"],
            AccountId=record.get("AccountId"),
            account_name=(record.get("Account") or {}).get("Name"),
            StageName=record.get("StageName"),
            CloseDate=record.get("CloseDate"),
            Amount=record.get("Amount"),
            owner_name=(record.get("Owner") or {}).get("Name"),
            NextStep=record.get("NextStep"),
            LastModifiedDate=record.get("LastModifiedDate"),
        )

    def create_task_for_opportunity(
        self,
        opportunity_id: str,
        subject: str,
        description: str,
        approved: bool,
    ) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Salesforce Task creation requires explicit approval.")
        if self.settings.dry_run:
            return {"dry_run": True, "operation": "create_task", "opportunity_id": opportunity_id}
        if self.connection_mode == "salesforce_cli":
            raise PermissionError("The Salesforce CLI connection is read-only in this agent.")
        self.authenticate()
        payload = {
            "WhatId": opportunity_id,
            "Subject": subject,
            "Description": description,
            "Status": "Completed",
        }
        return self.sobject_post("Task", payload)

    def update_opportunity_next_step(
        self,
        opportunity_id: str,
        next_step: str,
        approved: bool,
    ) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Opportunity updates require explicit approval.")
        if self.settings.dry_run:
            return {
                "dry_run": True,
                "operation": "update_opportunity_next_step",
                "opportunity_id": opportunity_id,
            }
        if self.connection_mode == "salesforce_cli":
            raise PermissionError("The Salesforce CLI connection is read-only in this agent.")
        self.authenticate()
        return self.sobject_patch("Opportunity", opportunity_id, {"NextStep": next_step})

    def query(self, soql: str) -> list[dict[str, Any]]:
        self.authenticate()
        if self.connection_mode == "salesforce_cli":
            result = self._run_sf_json(
                [
                    "data",
                    "query",
                    "--target-org",
                    self.settings.salesforce_cli_alias,
                    "--query",
                    soql,
                ]
            )
            records = result.get("records", [])
            return records if isinstance(records, list) else []
        assert self.instance_url and self.access_token
        headers = {"Authorization": f"Bearer {self.access_token}"}
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{self.instance_url}/services/data/{self.settings.salesforce_api_version}/query",
                headers=headers,
                params={"q": soql},
            )
            response.raise_for_status()
        return response.json().get("records", [])

    def sobject_post(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.instance_url and self.access_token
        headers = {"Authorization": f"Bearer {self.access_token}"}
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.instance_url}/services/data/{self.settings.salesforce_api_version}/sobjects/{name}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        return response.json()

    def sobject_patch(
        self,
        name: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert self.instance_url and self.access_token
        headers = {"Authorization": f"Bearer {self.access_token}"}
        with httpx.Client(timeout=30) as client:
            response = client.patch(
                f"{self.instance_url}/services/data/{self.settings.salesforce_api_version}/sobjects/{name}/{record_id}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        return {"success": response.status_code in {200, 204}}

    def _cli_path(self) -> str | None:
        configured_path = self.settings.salesforce_cli_path.strip()
        if not configured_path:
            return None
        explicit = Path(configured_path).expanduser()
        if explicit.is_file():
            return str(explicit)
        return shutil.which(configured_path)

    def _run_sf_json(self, arguments: list[str]) -> dict[str, Any]:
        executable = self._cli_path()
        if not executable:
            raise RuntimeError("Salesforce CLI command was not found.")
        completed = subprocess.run(
            [executable, *arguments, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Salesforce CLI returned an unreadable response.") from exc
        if completed.returncode != 0 or payload.get("status") not in {0, None}:
            message = payload.get("message") or "Salesforce CLI request failed."
            raise RuntimeError(str(message))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Salesforce CLI response did not contain an object result.")
        return result


def candidate_search_terms(intelligence: MeetingIntelligence) -> list[str]:
    terms: list[str] = []
    account_guess = clean_search_term(intelligence.customer_account_guess or "")
    if account_guess and not is_generic_search_term(account_guess):
        terms.append(account_guess)

    if "1:1" not in intelligence.meeting_title and not title_is_people_only_sync(intelligence):
        title_tokens = [
            token
            for token in TOKEN_RE.findall(intelligence.meeting_title)
            if is_useful_title_token(token)
        ]
        if len(title_tokens) >= 2:
            terms.append(" ".join(title_tokens[:5]))
        terms.extend(
            token
            for index, token in enumerate(title_tokens)
            if len(token.strip("-_/+")) >= 4
            or (index == 0 and token.isupper())
        )

    unique: list[str] = []
    seen: set[str] = set()
    for value in terms:
        cleaned = clean_search_term(value)
        key = cleaned.lower()
        if not cleaned or key in seen or is_generic_search_term(cleaned):
            continue
        seen.add(key)
        unique.append(cleaned)
        if len(unique) >= 6:
            break
    return unique


def is_useful_title_token(value: str) -> bool:
    normalized = value.lower().strip("-_/+")
    if len(normalized) < 3 or normalized in TITLE_STOP_WORDS:
        return False
    return not normalized.isdigit() or len(normalized) >= 6


def clean_search_term(value: str) -> str:
    cleaned = value.replace("%", " ").replace("_", " ")
    return " ".join(cleaned.split()).strip(" -/+&")


def is_generic_search_term(value: str) -> bool:
    normalized = value.lower().strip()
    return normalized in TITLE_STOP_WORDS or normalized.startswith("pasted transcript")


def is_internal_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[-1].lower() if "@" in value else ""
    return domain in INTERNAL_EMAIL_DOMAINS


def title_is_people_only_sync(intelligence: MeetingIntelligence) -> bool:
    if "weekly sync" not in intelligence.meeting_title.lower():
        return False
    title_tokens = {
        token.lower()
        for token in TOKEN_RE.findall(intelligence.meeting_title)
        if is_useful_title_token(token)
    }
    attendee_tokens = {
        token.lower()
        for attendee in intelligence.attendees
        for token in TOKEN_RE.findall(attendee.name or "")
    }
    return bool(title_tokens and title_tokens <= attendee_tokens)


def name_match_clause(value: str) -> str:
    tokens = [clean_search_term(token) for token in TOKEN_RE.findall(value)]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ValueError("Salesforce name match requires at least one search token.")
    clauses = [f"Name LIKE {quote_soql('%' + token + '%')}" for token in tokens]
    return "(" + " AND ".join(clauses) + ")"


def build_proposed_updates(intelligence: MeetingIntelligence) -> ProposedSalesforceUpdates:
    return ProposedSalesforceUpdates(
        create_task={
            "Subject": f"Meeting notes: {intelligence.meeting_title}",
            "Description": intelligence.executive_summary,
            "Status": "Completed",
        },
        opportunity_updates={"NextStep": intelligence.proposed_next_step},
    )


def quote_soql(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
