"""Runtime configuration. Every knob lives in .env so nothing is hardcoded."""
from __future__ import annotations

import pathlib

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    openai_api_key: str = ""

    # Model tiers. Extraction is the only quality-critical call; everything
    # else reasons over already-structured data, so it runs on the cheap tier.
    extractor_model: str = "gpt-4.1"
    cheap_model: str = "gpt-4.1-mini"

    # Hard stops. These are enforced in code, not merely hoped for.
    max_usd_per_document: float = 0.25
    max_llm_calls_per_run: int = 12
    llm_timeout_seconds: int = 90

    # OCR gives scanned pages a verification corpus that is independent of
    # the extracting model. Turn it off to fall back to LLM transcription,
    # which is weaker evidence and is trusted less accordingly.
    use_ocr: bool = True

    auto_approve_min_confidence: float = 0.85
    uncertain_below: float = 0.60

    @property
    def data_dir(self) -> pathlib.Path:
        d = ROOT / "data"
        d.mkdir(exist_ok=True)
        return d

    @property
    def render_dir(self) -> pathlib.Path:
        d = self.data_dir / "renders"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> pathlib.Path:
        return self.data_dir / "nova.db"


settings = Settings()
