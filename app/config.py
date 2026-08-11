import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


def default_tribble_db_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Tribble Desktop" / "tribble.db"
    return Path.home() / "Library" / "Application Support" / "Tribble Desktop" / "tribble.db"


class Settings(BaseSettings):
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    openai_fallback_to_local: bool = Field(default=True, alias="OPENAI_FALLBACK_TO_LOCAL")

    zoom_account_id: str | None = Field(default=None, alias="ZOOM_ACCOUNT_ID")
    zoom_client_id: str | None = Field(default=None, alias="ZOOM_CLIENT_ID")
    zoom_client_secret: str | None = Field(default=None, alias="ZOOM_CLIENT_SECRET")
    zoom_user_id: str = Field(default="me", alias="ZOOM_USER_ID")
    transcribe_audio: bool = Field(default=False, alias="TRANSCRIBE_AUDIO")

    salesforce_client_id: str | None = Field(default=None, alias="SALESFORCE_CLIENT_ID")
    salesforce_client_secret: str | None = Field(default=None, alias="SALESFORCE_CLIENT_SECRET")
    salesforce_username: str | None = Field(default=None, alias="SALESFORCE_USERNAME")
    salesforce_password: str | None = Field(default=None, alias="SALESFORCE_PASSWORD")
    salesforce_security_token: str | None = Field(default=None, alias="SALESFORCE_SECURITY_TOKEN")
    salesforce_login_url: str = Field(
        default="https://login.salesforce.com", alias="SALESFORCE_LOGIN_URL"
    )
    salesforce_api_version: str = Field(default="v60.0", alias="SALESFORCE_API_VERSION")
    salesforce_cli_enabled: bool = Field(default=False, alias="SALESFORCE_CLI_ENABLED")
    salesforce_cli_alias: str = Field(default="uipath", alias="SALESFORCE_CLI_ALIAS")
    salesforce_cli_path: str = Field(default="sf", alias="SALESFORCE_CLI_PATH")

    quip_access_token: str | None = Field(default=None, alias="QUIP_ACCESS_TOKEN")
    quip_base_url: str = Field(default="https://platform.quip.com", alias="QUIP_BASE_URL")
    quip_folder_id: str | None = Field(default=None, alias="QUIP_FOLDER_ID")

    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_transcripts: bool = Field(default=False, alias="LOG_TRANSCRIPTS")
    review_base_url: str = Field(default="http://127.0.0.1:8000", alias="REVIEW_BASE_URL")
    data_dir: Path = Field(default=Path("./data/runs"), alias="DATA_DIR")
    out_dir: Path = Field(default=Path("./out"), alias="OUT_DIR")
    zoom_download_dir: Path = Field(default=Path("./data/zoom"), alias="ZOOM_DOWNLOAD_DIR")
    tribble_db_path: Path = Field(
        default_factory=default_tribble_db_path,
        alias="TRIBBLE_DB_PATH",
    )
    tribble_download_dir: Path = Field(
        default=Path("./data/tribble"),
        alias="TRIBBLE_DOWNLOAD_DIR",
    )
    tribble_timezone: str = Field(
        default="America/New_York",
        alias="TRIBBLE_TIMEZONE",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
