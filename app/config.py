from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Amazon — amazon_totp_secret fully bypasses 2FA, the most sensitive value here
    amazon_email: str = ""
    amazon_password: str = ""
    amazon_totp_secret: str = ""

    # YNAB
    ynab_personal_access_token: str = ""
    ynab_budget_id: str = "last-used"
    ynab_account_id: str = ""
    ynab_match_window_days: int = 5
    ynab_only_match_uncategorized: bool = True
    ynab_amazon_payee_filters: str = "Amazon,AMZN"
    # Off by default: whether create_transaction() is allowed to POST a brand-new
    # transaction for an order with no bank-fed match (match_status == 'no_candidate').
    # An explicit, deliberate opt-in — not something that should happen just because
    # a bank sync gap left orders unmatched.
    ynab_allow_create_without_match: bool = False

    # LLM — pluggable
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    openai_compatible_base_url: str = "http://localhost:11434/v1"
    openai_compatible_api_key: str = ""
    openai_compatible_model: str = "llama3.1"

    # Scraper
    selenium_remote_url: str = ""
    scrape_headless: bool = True

    # Dev-only safety bypasses — must stay false against a live budget
    allow_reset: bool = False
    allow_reapply: bool = False

    # App
    pipeline_schedule_cron: str = "0 7 * * *"
    database_path: str = "./data/app.db"
    receipts_dir: str = "./data/receipts_html"
    dashboard_port: int = 8420
    log_level: str = "INFO"

    @property
    def ynab_amazon_payee_filter_list(self) -> list[str]:
        return [p.strip() for p in self.ynab_amazon_payee_filters.split(",") if p.strip()]


settings = Settings()
