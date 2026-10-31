"""Bounded, in-memory speech pages tied to an already authorized answer job."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

MAX_ANSWER = 6000
MAX_PAGE = 2000
MAX_ANSWERS = 32
MAX_REQUESTS = 128
CONTINUE_HINT = "Für den nächsten Teil sage: Alexa, frage Lumi Chat: Antwort."
PENDING_TEXT = "Die Antwort wird noch erstellt. Sage in einem Moment: Alexa, frage Lumi Chat: Antwort."
ERROR_MESSAGES = {
    "timeout": "Die Antwort hat zu lange gedauert. Bitte stelle deine Frage erneut.",
    "unauthenticated": "Ich kann die Anmeldung von Lumi gerade nicht bestätigen. Bitte prüfe Codex Voice.",
    "busy": "Lumi bearbeitet gerade andere Anfragen. Bitte versuche es in einem Moment erneut.",
    "unavailable": "Der Gesprächsdienst ist gerade nicht erreichbar. Bitte versuche es später erneut.",
}


def split_answer(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_ANSWER:
        raise ValueError("Invalid answer")
    remaining = " ".join(text.split())
    pages = []
    while len(remaining) > MAX_PAGE:
        budget = MAX_PAGE - len(CONTINUE_HINT) - 1
        sentences = [match.end() for match in re.finditer(r'[.!?](?:[\"»”’“\')\]]*)(?=\s|$)', remaining[:budget])]
        boundary = sentences[-1] if sentences else remaining.rfind(" ", 0, budget + 1)
        if boundary <= 0:
            boundary = budget
        pages.append(remaining[:boundary].rstrip() + " " + CONTINUE_HINT)
        remaining = remaining[boundary:].lstrip()
    pages.append(remaining)
    return tuple(pages)


@dataclass
class PagedAnswer:
    job_id: str
    expires: float
    pages: tuple[str, ...]
    index: int = 0
    presented: bool = False
    requests: dict[str, tuple[bool, int]] = field(default_factory=dict)


class AnswerPages:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.answers = {}

    def expire(self):
        now = self.clock()
        self.answers = {key: value for key, value in self.answers.items() if value.expires > now}

    def clear(self, principal):
        self.answers.pop(principal, None)

    def first(self, principal, job, text):
        self.expire()
        existing = self.answers.get(principal)
        if existing is None or existing.job_id != job.job_id:
            if job.expires <= self.clock():
                raise ValueError("Expired answer")
            if existing is None and len(self.answers) >= MAX_ANSWERS:
                raise ValueError("Answer capacity reached")
            self.answers[principal] = PagedAnswer(job.job_id, job.expires, split_answer(text))
        return self.answers[principal].pages[0]

    def available(self, principal):
        self.expire()
        return principal in self.answers

    def mark_presented(self, principal, job):
        answer = self.answers.get(principal)
        if answer is not None and answer.job_id == job.job_id:
            answer.presented = True

    def select(self, principal, request_id, *, repeat=False, first=False):
        self.expire()
        answer = self.answers.get(principal)
        if answer is None:
            raise ValueError("Answer unavailable")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 1024:
            raise ValueError("Invalid answer request")
        previous = answer.requests.get(request_id)
        if previous is not None:
            if previous[0] != repeat:
                raise ValueError("Answer request mismatch")
            return answer.pages[previous[1]]
        if len(answer.requests) >= MAX_REQUESTS:
            raise ValueError("Answer request capacity reached")
        if not repeat and not first:
            answer.index = min(answer.index + 1, len(answer.pages) - 1)
        answer.requests[request_id] = (repeat, answer.index)
        answer.presented = True
        return answer.pages[answer.index]
