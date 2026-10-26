"""Configuration loader: reads environment variables (and .env) once, validates them."""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("internflow.config")


class Config:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    DATABASE_PATH = os.getenv("DATABASE_PATH", "internflow.db")

    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SENDER_NAME = os.getenv("SENDER_NAME", "Internship Program Team")

    @classmethod
    def smtp_ready(cls) -> bool:
        """True when enough SMTP settings exist to send real email."""
        return bool(cls.SMTP_USER and cls.SMTP_PASSWORD)

    @classmethod
    def validate(cls) -> None:
        """Warn early about missing settings instead of failing mid-request."""
        if not cls.GEMINI_API_KEY:
            log.warning("GEMINI_API_KEY is not set - /api/generate will return an error.")
        if not cls.smtp_ready():
            log.info("SMTP not configured - the app will run in draft-only mode.")
