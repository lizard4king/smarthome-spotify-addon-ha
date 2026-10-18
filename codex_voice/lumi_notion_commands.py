"""Deterministic explicit Notion commands; never infer text or destinations."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Protocol
from uuid import UUID


class NotionWriterProtocol(Protocol):
    def create_note(self, page_id: str, title: str, text: str, request_id: str,
                    *, explicit_instruction: bool, title_already_in_text: bool = False) -> dict: ...

    def update_note(self, page_id: str, block_id: str, text: str, request_id: str,
                    *, explicit_instruction: bool) -> dict: ...


_CREATE = re.compile(r"Notiz in (?P<target>[^:\n]+?) speichern\s*:\s*(?P<text>.+)", re.I | re.S)
_UPDATE = re.compile(
    r"Notiz in (?P<target>[^:\n]+?) ändern Block "
    r"(?P<block>[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\s*:\s*(?P<text>.+)",
    re.I | re.S,
)
_INTENT = re.compile(r"\b(?:notion|notiz(?:en)?)\b.*\b(?:speicher\w*|änder\w*|schreib\w*|anleg\w*|erstell\w*|aktualisier\w*)\b|"
                     r"\b(?:speicher\w*|änder\w*|schreib\w*|anleg\w*|erstell\w*|aktualisier\w*)\b.*\b(?:notion|notiz(?:en)?)\b", re.I | re.S)
_HELP = ("Bitte gib einen ausdrücklichen Befehl mit freigegebenem Ziel und Inhalt an: "
         "‚Notiz in Haus speichern: Filter wechseln‘. Zum Ändern: "
         "‚Notiz in Haus ändern Block <UUID>: Neuer Text‘.")


class NotionCommandService:
    """Resolve aliases only from trusted server configuration."""

    def __init__(self, writer: NotionWriterProtocol, targets: Mapping[str, str]):
        self.writer = writer
        self.targets: dict[str, str] = {}
        for alias, page_id in targets.items():
            key = alias.strip().casefold()
            if not key or key in self.targets:
                raise ValueError("Notion target aliases must be unique and nonempty")
            self.targets[key] = str(UUID(page_id))

    def handle(self, question: str, request_id: str, *, authorized: bool) -> str | None:
        question = question.strip()
        match = _CREATE.fullmatch(question)
        update = _UPDATE.fullmatch(question)
        if match is None and update is None:
            return _HELP if _INTENT.search(question) else None
        if not authorized:
            return "Für diesen Notion-Schreibauftrag fehlt die Freigabe. Es wurde nichts geschrieben."
        if not isinstance(request_id, str) or not request_id.strip():
            return "Die eindeutige Auftragskennung fehlt. Es wurde nichts geschrieben."
        command = update or match
        assert command is not None
        page_id = self.targets.get(command['target'].strip().casefold())
        if page_id is None:
            return "Dieses Notion-Ziel ist nicht freigegeben. Bitte nenne ein freigegebenes Ziel."
        content = command['text'].strip()
        if not content:
            return _HELP
        try:
            if update is not None:
                result = self.writer.update_note(page_id, str(UUID(update['block'])), content,
                                                 request_id, explicit_instruction=True)
            else:
                result = self.writer.create_note(page_id, content.splitlines()[0][:120], content,
                                                 request_id, explicit_instruction=True,
                                                 title_already_in_text=True)
        except Exception:
            return "Der Notion-Schreibstatus ist unklar. Bitte prüfe den Auftrag, bevor du ihn erneut erteilst."
        status = result.get('status') if isinstance(result, dict) else None
        if status == 'confirmed':
            return "Die Notiz wurde in Notion geändert." if update else "Die Notiz wurde in Notion gespeichert."
        if status == 'rejected':
            return "Notion hat den Schreibauftrag abgelehnt. Es wurde kein Erfolg bestätigt."
        return "Der Notion-Schreibstatus ist unklar. Bitte prüfe den Auftrag, bevor du ihn erneut erteilst."
