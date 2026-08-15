import json
import logging
import re
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# Matches a whole response wrapped in a ``` / ```json ... ``` fence, start to
# end. Anchored (not a bare .strip("`")) so a legitimate trailing backtick
# inside real content isn't eaten too (docs/IMPROVEMENTS.md Extra 2).
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?(.*?)\n?```$", re.DOTALL)

EXTRACTION_PROMPT = """\
You are a helpful assistant that reads Amazon order receipts and extracts a structured
summary as JSON. Output must match this schema exactly, with no other text before or after:

{"grand_total": "decimal string", "subtotal": "decimal string", "total_before_tax": "decimal string", \
"date": "YYYY-MM-DD", "items": [{"short_name": "string, <=64 chars", "title": "string", \
"price": "decimal string", "category": "one of: %s"}]}
"""


class LLMProvider(Protocol):
    def extract_receipt(self, receipt_text: str, category_names: list[str]) -> dict: ...


class BaseLLMProvider:
    """Retries once on a malformed JSON response before giving up. Anthropic models
    rarely need this; OpenAI-compatible local models (Ollama/vLLM) enforce structured
    output less reliably, so the retry lives here rather than per-provider."""

    retries = 1

    def _call_once(self, receipt_text: str, category_names: list[str]) -> str:
        raise NotImplementedError

    def extract_receipt(self, receipt_text: str, category_names: list[str]) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            raw = self._call_once(receipt_text, category_names)
            try:
                return _parse_json_response(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "LLM response was not valid JSON (attempt %d/%d): %s",
                    attempt + 1,
                    self.retries + 1,
                    exc,
                )
        raise ValueError(f"LLM did not return valid JSON after {self.retries + 1} attempt(s): {last_error}")


class AnthropicProvider(BaseLLMProvider):
    def __init__(self):
        import anthropic

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    def _call_once(self, receipt_text: str, category_names: list[str]) -> str:
        prompt = EXTRACTION_PROMPT % ", ".join(category_names)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=prompt,
            messages=[{"role": "user", "content": receipt_text}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class OpenAICompatibleProvider(BaseLLMProvider):
    """Talks to any OpenAI-chat-completions-compatible endpoint — a locally hosted
    model server (e.g. Ollama, vLLM) via OPENAI_COMPATIBLE_BASE_URL, not just OpenAI."""

    def __init__(self):
        from openai import OpenAI

        self._client = OpenAI(
            base_url=settings.openai_compatible_base_url,
            api_key=settings.openai_compatible_api_key or "not-needed",
        )
        self._model = settings.openai_compatible_model

    def _call_once(self, receipt_text: str, category_names: list[str]) -> str:
        prompt = EXTRACTION_PROMPT % ", ".join(category_names)
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": receipt_text},
            ],
        )
        return response.choices[0].message.content


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1)
    return json.loads(text.strip())


_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def get_provider() -> LLMProvider:
    provider_cls = _PROVIDERS.get(settings.llm_provider)
    if provider_cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (expected one of {list(_PROVIDERS)})"
        )
    return provider_cls()
