import logging
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from app.config import settings

logger = logging.getLogger(__name__)


def build_driver():
    """Builds a Chrome webdriver, remote (Docker's selenium/standalone-chrome) if
    SELENIUM_REMOTE_URL is set, otherwise local via chromedriver_autoinstaller.
    Enables a virtual WebAuthn authenticator so passkey challenges during Amazon
    login don't block headless automation (ported from vendor/amazon_orders_webscraper)."""
    options = webdriver.ChromeOptions()
    if settings.scrape_headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")

    # Persistent profile so Amazon sees the same returning device across runs
    # instead of a brand-new one every time — a fresh profile every run is a
    # strong "unrecognized device" signal, and is almost certainly why Amazon
    # was escalating to an extra SMS challenge on top of TOTP. Passed for both
    # the local and remote executor: for local, the directory lives on this
    # host and is created below; for remote (Docker), the path is interpreted
    # inside the selenium container, so it only actually persists once a
    # matching volume is mounted there — the app side is ready either way.
    options.add_argument(f"--user-data-dir={settings.chrome_profile_dir}")

    if settings.selenium_remote_url:
        logger.info("Connecting to remote Selenium at %s", settings.selenium_remote_url)
        driver = webdriver.Remote(command_executor=settings.selenium_remote_url, options=options)
    else:
        Path(settings.chrome_profile_dir).mkdir(parents=True, exist_ok=True)

        import os

        import chromedriver_autoinstaller

        # chromedriver_autoinstaller's version-check call uses urllib directly
        # against the system OpenSSL cert store. python.org's macOS installer
        # doesn't populate that store (a well-known gotcha — "Install
        # Certificates.command" is the usual manual fix), which fails this
        # specific call even though requests/httpx/anthropic all work fine via
        # their own bundled certifi certs. setdefault so an operator's own
        # SSL_CERT_FILE, if set, always wins.
        try:
            import certifi

            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        except ImportError:
            pass

        chrome_driver_path = chromedriver_autoinstaller.install()
        service = Service(executable_path=chrome_driver_path)
        driver = webdriver.Chrome(service=service, options=options)

    driver.execute_cdp_cmd("WebAuthn.enable", {})
    driver.execute_cdp_cmd(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": False,
                "hasUserVerification": False,
                "isUserVerified": False,
            }
        },
    )
    return driver
