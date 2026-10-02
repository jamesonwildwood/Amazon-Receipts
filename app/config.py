from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Amazon — amazon_totp_secret fully bypasses 2FA, the most sensitive value here.
    # Legacy single-account config: back-compat only. Preferred mechanism is
    # amazon_accounts.toml (app/accounts.py), which supports multiple accounts;
    # these env vars are synthesized into a single "default" account when the
    # toml is absent (docs/IMPROVEMENTS.md 3.1).
    amazon_email: str = ""
    amazon_password: str = ""
    amazon_totp_secret: str = ""
    # Docker-mountable path so the toml can be mounted read-only separately from .env.
    amazon_accounts_path: str = "./amazon_accounts.toml"

    # YNAB
    ynab_personal_access_token: str = ""
    ynab_budget_id: str = "last-used"
    ynab_account_id: str = ""
    # The match window is asymmetric: ynab_match_window_days *before* the
    # order date (a charge can't really predate its order; this only absorbs
    # bank-feed date quirks) and ynab_match_forward_days *after* it. Amazon
    # charges at shipment, not at order time -- Subscribe & Save in particular
    # lands 14-19 days after the order date, which the old symmetric ±10-day
    # window missed every month. Amount-exact + payee + uncategorized +
    # claim-ledger filters keep the wider forward window precise rather than
    # loose; the forward bound still matters because recurring S&S items
    # repeat the exact same amount every ~30 days.
    ynab_match_window_days: int = 10
    ynab_match_forward_days: int = 25
    ynab_only_match_uncategorized: bool = True
    ynab_amazon_payee_filters: str = "Amazon,AMZN"
    # Off by default: whether create_transaction() is allowed to POST a brand-new
    # transaction for an order with no bank-fed match (match_status == 'no_candidate').
    # An explicit, deliberate opt-in — not something that should happen just because
    # a bank sync gap left orders unmatched.
    ynab_allow_create_without_match: bool = False
    # On by default (docs/IMPROVEMENTS.md Part 6): a single-candidate match is
    # applied immediately, and categorization of whatever the resolver couldn't
    # place happens in YNAB's own queue, not a second queue here. Goes through
    # the exact same guarded apply_patch() the dashboard's Approve button uses —
    # no new write path, every existing guard still applies. Kept as a setting
    # purely as a kill switch: false parks matches in pending_review for a
    # manual Approve click.
    ynab_auto_apply: bool = True

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
    # Persists Chrome's cookies/session across runs so Amazon recognizes this
    # as the same returning device instead of a brand-new one every time —
    # without this, every single run looked unrecognized to Amazon's own risk
    # system, which is almost certainly why it escalated to an extra SMS
    # challenge on top of TOTP. Gitignored via the existing data/ entry.
    chrome_profile_dir: str = "./data/chrome_profile"

    # Dev-only safety bypasses — must stay false against a live budget
    allow_reset: bool = False
    allow_reapply: bool = False

    # Notifications (docs/IMPROVEMENTS.md 5.1) — plain SMTP+STARTTLS (e.g. a
    # Gmail app password), off by default. Empty NOTIFY_SMTP_HOST means
    # "notifications disabled", not "misconfigured" — a fresh install stays
    # quiet until someone opts in. A notifier failure must never fail or
    # delay the pipeline (see app/notify.py).
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password: str = ""
    notify_email_from: str = ""  # defaults to notify_smtp_user when empty (see app/notify.py)
    notify_email_to: str = ""
    notify_dashboard_url: str = "http://localhost:8420"

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
