"""Strict, side-effect-free request boundary for the Alexa conversation skill."""

from __future__ import annotations

import secrets
import re
from dataclasses import dataclass
from typing import Mapping, Protocol


ASSISTANT_NAME = "Lumi"
INVOCATION_NAME = "lumi chat"
ERLENE_GREETING = "Olá, Erlene. Eu sou a Lumi. O que você gostaria de saber?"
ERLENE_REPROMPT = "O que você gostaria de saber?"
QUESTION_INTENT = "AskChatGPTIntent"
QUESTION_SLOT = "Question"
MAX_APPLICATION_ID_CHARACTERS = 256
MAX_SPEECH_CHARACTERS = 480


class ConversationResponder(Protocol):
    """Read-only text responder used after Alexa request validation."""

    def reply(self, prompt: str) -> str:
        """Return one bounded spoken answer."""


class AlexaConversationError(ValueError):
    """Raised when an Alexa request crosses the configured skill boundary."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One validated response with explicit Alexa session behavior."""

    speech: str
    should_end_session: bool
    reprompt: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "speech",
            _spoken_text(self.speech, "Die Alexa-Antwort"),
        )
        if not isinstance(self.should_end_session, bool):
            raise AlexaConversationError("Der Alexa-Sitzungsstatus ist ungültig.")
        if self.reprompt is not None:
            object.__setattr__(
                self,
                "reprompt",
                _spoken_text(self.reprompt, "Die Alexa-Rückfrage"),
            )

    def to_alexa_response(self) -> dict[str, object]:
        response: dict[str, object] = {
            "outputSpeech": {"type": "PlainText", "text": self.speech},
            "shouldEndSession": self.should_end_session,
        }
        if self.reprompt is not None:
            response["reprompt"] = {
                "outputSpeech": {"type": "PlainText", "text": self.reprompt}
            }
        return {"version": "1.0", "response": response}


def handle_alexa_conversation(
    event: Mapping[str, object],
    *,
    expected_application_id: str,
    responder: ConversationResponder,
    erlene_person_id: str | None = None,
) -> ConversationTurn:
    """Validate one custom-skill event and produce a read-only conversation turn."""

    _verify_application_id(event, expected_application_id)
    request = _mapping(event.get("request"), "Alexa request")
    request_type = request.get("type")

    if request_type == "LaunchRequest":
        if erlene_person_id and _person_id(event) == erlene_person_id:
            return ConversationTurn(
                ERLENE_GREETING,
                should_end_session=False,
                reprompt=ERLENE_REPROMPT,
            )
        prompt = "Was möchtest du wissen?"
        return ConversationTurn(
            "Hallo, ich bin Lumi. Was möchtest du wissen?",
            should_end_session=False,
            reprompt=prompt,
        )
    if request_type == "SessionEndedRequest":
        return ConversationTurn("Bis bald.", should_end_session=True)
    if request_type != "IntentRequest":
        raise AlexaConversationError("Der Alexa-Anfragetyp ist nicht zugelassen.")

    intent = _mapping(request.get("intent"), "Alexa intent")
    intent_name = intent.get("name")
    if intent_name in {"AMAZON.CancelIntent", "AMAZON.StopIntent"}:
        return ConversationTurn("Bis bald.", should_end_session=True)
    if intent_name == "AMAZON.RepeatIntent":
        prompt = "Bitte stelle deine letzte Frage noch einmal."
        return ConversationTurn(prompt, should_end_session=False, reprompt=prompt)
    if intent_name == "AMAZON.HelpIntent":
        prompt = "Stelle mir eine kurze Frage oder sage Stopp."
        return ConversationTurn(prompt, should_end_session=False, reprompt=prompt)
    if intent_name != QUESTION_INTENT:
        prompt = "Das habe ich nicht verstanden. Was möchtest du wissen?"
        return ConversationTurn(prompt, should_end_session=False, reprompt=prompt)

    slots = _mapping(intent.get("slots"), "Alexa slots")
    question_slot = _mapping(slots.get(QUESTION_SLOT), "Alexa question slot")
    question = question_slot.get("value")
    if not isinstance(question, str) or not question.strip():
        prompt = "Welche Frage soll ich beantworten?"
        return ConversationTurn(prompt, should_end_session=False, reprompt=prompt)

    try:
        speech = responder.reply(question)
    except ValueError:
        speech = "Der Gesprächsdienst ist gerade nicht erreichbar."
    return ConversationTurn(
        speech,
        should_end_session=False,
        reprompt="Möchtest du noch etwas wissen?",
    )


def _verify_application_id(
    event: Mapping[str, object], expected_application_id: str
) -> None:
    if (
        not isinstance(expected_application_id, str)
        or not expected_application_id.strip()
        or len(expected_application_id) > MAX_APPLICATION_ID_CHARACTERS
    ):
        raise AlexaConversationError("Die erwartete Alexa-Skill-ID ist ungültig.")
    session = _mapping(event.get("session"), "Alexa session")
    application = _mapping(session.get("application"), "Alexa application")
    actual = application.get("applicationId")
    if not isinstance(actual, str) or not secrets.compare_digest(
        actual, expected_application_id
    ):
        raise AlexaConversationError("Die Alexa-Anfrage gehört nicht zu diesem Skill.")


def _person_id(event: Mapping[str, object]) -> str | None:
    """Return Alexa's pseudonymous person ID, if personalization supplied it."""

    context = event.get("context")
    if isinstance(context, Mapping):
        system = context.get("System")
        if isinstance(system, Mapping):
            person = system.get("person")
            if isinstance(person, Mapping):
                person_id = person.get("personId")
                if isinstance(person_id, str) and person_id.strip():
                    return person_id.strip()
    # Some Alexa request variants expose the same object under session.person.
    session = event.get("session")
    if isinstance(session, Mapping):
        person = session.get("person")
        if isinstance(person, Mapping):
            person_id = person.get("personId")
            if isinstance(person_id, str) and person_id.strip():
                return person_id.strip()
    return None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AlexaConversationError(f"{label} fehlt oder ist ungültig.")
    return value


def _spoken_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AlexaConversationError(f"{label} ist kein Text.")
    text = re.sub(r"\s+", " ", value).strip()
    if not text or len(text) > MAX_SPEECH_CHARACTERS:
        raise AlexaConversationError(f"{label} ist leer oder zu lang.")
    return text
