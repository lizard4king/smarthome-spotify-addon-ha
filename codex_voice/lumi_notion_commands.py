"""Deterministic explicit Notion commands; never infer text or destinations."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import re
from typing import Protocol
import unicodedata
from uuid import UUID


class NotionWriterProtocol(Protocol):
    def create_note(self, page_id: str, title: str, text: str, request_id: str,
                    *, explicit_instruction: bool, title_already_in_text: bool = False) -> dict: ...

    def update_note(self, page_id: str, block_id: str, text: str, request_id: str,
                    *, explicit_instruction: bool) -> dict: ...


_PREFIX = re.compile(r"\AMeine Frage ist(?:\s*:\s*|\s+|$)", re.I)
_CREATE = re.compile(
    r"Notiz in (?P<target>[^:\n]+?) speichern"
    r"(?:\s*:\s*|\s+mit\s+(?:dem\s+)?Inhalt\s+)(?P<text>.+)",
    re.I | re.S,
)
_UPDATE = re.compile(
    r"Notiz in (?P<target>[^:\n]+?) ändern Block "
    r"(?P<block>[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\s*:\s*(?P<text>.+)",
    re.I | re.S,
)
_INTENT = re.compile(r"\b(?:notion|notiz(?:en)?)\b.*\b(?:speicher\w*|änder\w*|schreib\w*|anleg\w*|erstell\w*|aktualisier\w*)\b|"
                     r"\b(?:speicher\w*|änder\w*|schreib\w*|anleg\w*|erstell\w*|aktualisier\w*)\b.*\b(?:notion|notiz(?:en)?)\b", re.I | re.S)
_HELP = ("Es wurde nichts geschrieben. Bitte nenne Ziel und Inhalt zusammen in einem Satz "
         "als ausdrücklichen Befehl: "
         "‚Notiz in Haus speichern mit Inhalt Filter wechseln‘. Zum Ändern: "
         "‚Notiz in Haus ändern Block <UUID>: Neuer Text‘.")

_MONTHS = {name: number for number, name in enumerate((
    'januar', 'februar', 'märz', 'april', 'mai', 'juni', 'juli', 'august',
    'september', 'oktober', 'november', 'dezember'), start=1)}
_DATE_SUFFIXES = (
    re.compile(r'(?:^|\s)(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})\Z'),
    re.compile(r'(?:^|\s)(?P<year>[0-9]{4})\s+(?P<month>[0-9]{1,2})\s+(?P<day>[0-9]{1,2})\Z'),
    re.compile(r'(?:^|\s)(?P<day>[0-9]{1,2})\.(?P<month>[0-9]{1,2})\.(?P<year>[0-9]{4})\Z'),
    re.compile(r'(?:^|\s)(?P<day>[0-9]{1,2})\s+(?P<month>[0-9]{1,2})\s+(?P<year>[0-9]{4})\Z'),
    re.compile(r'(?:^|\s)(?P<day>[0-9]{1,2})\.?\s+(?P<month_name>'
               + '|'.join(_MONTHS) + r')\s+(?P<year>[0-9]{4})\Z'),
)


def _canonical_target(value: str) -> tuple | None:
    """Equate complete title spellings; never infer missing date/title parts.

    Date and ordinary text keys have separate tags. Invalid calendar dates in
    an otherwise recognized date suffix cannot become an ordinary text target.
    """
    value = unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold()).strip()
    for pattern in _DATE_SUFFIXES:
        match = pattern.search(value)
        if match is None:
            continue
        fields = match.groupdict()
        month = (_MONTHS[fields['month_name']] if fields.get('month_name')
                 else int(fields['month']))
        try:
            day = date(int(fields['year']), month, int(fields['day']))
        except ValueError:
            return None
        prefix = ''.join(value[:match.start()].split())
        return ('date', prefix, day)
    return ('text', ''.join(value.split()))


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
        groups: dict[tuple, set[str]] = {}
        for alias, page_id in self.targets.items():
            key = _canonical_target(alias)
            if key is not None:
                groups.setdefault(key, set()).add(page_id)
        # Multiple aliases for one page are safe. Multiple pages for one key
        # block every spelling, including an otherwise exact title match.
        self._canonical_targets = {key: next(iter(ids)) for key, ids in groups.items()
                                   if len(ids) == 1}

    def handle(self, question: str, request_id: str, *, authorized: bool) -> str | None:
        # Strip this one fixed voice introduction once; embedded or quoted commands
        # must still fail the fullmatch below. No dialogue state is retained.
        question = _PREFIX.sub("", question.strip(), count=1)
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
        page_id = self._canonical_targets.get(_canonical_target(command['target']))
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
