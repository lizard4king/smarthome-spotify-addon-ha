"""Read-only, stateless OpenAI response boundary for an Alexa custom skill."""

from __future__ import annotations

import os
import re
from typing import Any, Protocol


DEFAULT_CONVERSATION_MODEL = "gpt-5-mini"
MAX_INPUT_CHARACTERS = 500
MAX_OUTPUT_CHARACTERS = 480
MAX_OUTPUT_TOKENS = 160
REQUEST_TIMEOUT_SECONDS = 10.0

INSTRUCTIONS = """Du antwortest als rein lesende Assistentin Lumi.
Antworte in derselben Sprache wie die Frage. Unterstütze mindestens Deutsch
und Portugiesisch (brasilianisches Portugiesisch); wechsle nicht unnötig die
Sprache. Antworte knapp, natürlich und für Sprachausgabe geeignet, höchstens zwei Sätze.
Wenn du Erlene auf Portugiesisch ansprichst, sprich ihren Namen portugiesisch
als „Erléni“ aus (Betonung auf der ersten Silbe nach dem E; nicht englisch).
Behaupte niemals, ein Gerät gesteuert oder einen Zustand geprüft zu haben.
Fordere keine Kennwörter, Tokens, Adressen oder sonstigen Geheimnisse an.
Schlage bei Gerätewünschen höchstens vor, die getrennte Smart-Home-Steuerung zu nutzen.
"""


class ResponsesClient(Protocol):
    """Minimal Responses API surface required by the conversation adapter."""

    def create(self, **kwargs: Any) -> Any:
        """Create one model response."""


class ConversationConfigurationError(ValueError):
    """Raised when the optional OpenAI boundary is not configured safely."""


class ConversationResponseError(ValueError):
    """Raised when no safe spoken response can be produced."""


class OpenAIConversationResponder:
    """Produce one bounded answer without tools or server-side conversation state."""

    def __init__(self, responses: ResponsesClient, model: str = DEFAULT_CONVERSATION_MODEL) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ConversationConfigurationError("OPENAI_MODEL darf nicht leer sein.")
        self.responses = responses
        self.model = model.strip()

    @classmethod
    def from_environment(cls) -> OpenAIConversationResponder:
        """Build the adapter from process environment without reading secret files."""

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ConversationConfigurationError(
                "OPENAI_API_KEY ist nicht in der lokalen Prozessumgebung gesetzt."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConversationConfigurationError(
                'Das optionale Paket fehlt. Installiere es mit: pip install -e ".[openai]"'
            ) from exc

        client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=1,
        )
        return cls(client.responses, os.getenv("OPENAI_MODEL", DEFAULT_CONVERSATION_MODEL))

    def reply(self, prompt: str) -> str:
        """Return one plain-text answer; never expose a tool or device boundary."""

        clean_prompt = _bounded_text(prompt, "Die Frage", MAX_INPUT_CHARACTERS)
        try:
            response = self.responses.create(
                model=self.model,
                instructions=INSTRUCTIONS,
                input=clean_prompt,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
                tools=[],
            )
            output_text = response.output_text
        except Exception as exc:
            raise ConversationResponseError(
                "Der Gesprächsdienst ist gerade nicht erreichbar."
            ) from exc
        return _bounded_text(output_text, "Die Antwort", MAX_OUTPUT_CHARACTERS)


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConversationResponseError(f"{label} ist kein Text.")
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        raise ConversationResponseError(f"{label} darf nicht leer sein.")
    if len(text) > maximum:
        raise ConversationResponseError(f"{label} ist zu lang.")
    return text
