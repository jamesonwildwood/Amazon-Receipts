import logging

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

    if settings.selenium_remote_url:
        logger.info("Connecting to remote Selenium at %s", settings.selenium_remote_url)
        driver = webdriver.Remote(command_executor=settings.selenium_remote_url, options=options)
    else:
        import chromedriver_autoinstaller

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
