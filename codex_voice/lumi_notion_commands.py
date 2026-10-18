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
    r"Notiz (?:in|im) (?P<target>[^:\n]+?) speichern?"
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


def _compact(value: str) -> str:
    return ''.join(unicodedata.normalize('NFC', value.casefold()).split())


_UNITS = ('', 'ein', 'zwei', 'drei', 'vier', 'fünf', 'sechs', 'sieben', 'acht', 'neun')
_TEENS = ('zehn', 'elf', 'zwölf', 'dreizehn', 'vierzehn', 'fünfzehn', 'sechzehn',
          'siebzehn', 'achtzehn', 'neunzehn')
_TENS = ('', '', 'zwanzig', 'dreißig', 'vierzig', 'fünfzig', 'sechzig',
         'siebzig', 'achtzig', 'neunzig')


def _cardinal(number: int) -> str:
    if number < 10:
        return 'eins' if number == 1 else _UNITS[number]
    if number < 20:
        return _TEENS[number - 10]
    tens, units = divmod(number, 10)
    return (_UNITS[units] + 'und' if units else '') + _TENS[tens]


_CARDINALS = {_compact(_cardinal(n)): n for n in range(1, 100)}
_CARDINALS['ein'] = 1
_HUNDREDS = {_compact(_UNITS[n] + 'hundert'): n * 100 for n in range(1, 10)}
_HUNDREDS['hundert'] = 100
_THOUSANDS = {_compact(_UNITS[n] + 'tausend'): n * 1000 for n in range(1, 10)}
_THOUSANDS['tausend'] = 1000
_YEAR_HUNDREDS = {_compact(_cardinal(n) + 'hundert'): n * 100 for n in range(10, 100)}
_ORDINAL_STEMS = (
    '', 'erst', 'zweit', 'dritt', 'viert', 'fünft', 'sechst', 'siebt', 'acht', 'neunt',
    'zehnt', 'elft', 'zwölft', 'dreizehnt', 'vierzehnt', 'fünfzehnt', 'sechzehnt',
    'siebzehnt', 'achtzehnt', 'neunzehnt', 'zwanzigst', 'einundzwanzigst',
    'zweiundzwanzigst', 'dreiundzwanzigst', 'vierundzwanzigst', 'fünfundzwanzigst',
    'sechsundzwanzigst', 'siebenundzwanzigst', 'achtundzwanzigst', 'neunundzwanzigst',
    'dreißigst', 'einunddreißigst',
)
_ORDINALS = {_compact(stem + ending): n for n, stem in enumerate(_ORDINAL_STEMS)
             if n for ending in ('er', 'e', 'en', 'es', 'em')}
_ORDINALS.update({_compact('siebent' + ending): 7 for ending in ('er', 'e', 'en', 'es', 'em')})
_INVALID_ORDINALS = {_compact((_cardinal(n) + 'st' if n else 'nullt') + ending)
                     for n in (0, *range(32, 100)) for ending in ('er', 'e', 'en', 'es', 'em')}
_SPOKEN_NUMBERS = {**_CARDINALS, 'null': 0}
_NUMBER_PIECES = {_compact(word) for word in (*_UNITS[1:], *_TEENS, *_TENS[2:],
                                             'eins', 'und', 'tausend', 'hundert', 'null', 'minus', 'plus')}
_NUMBER_SEQUENCE = re.compile('(?:' + '|'.join(sorted(_NUMBER_PIECES, key=len, reverse=True)) + r')+\Z')


def _year_like(value: str) -> bool:
    return (bool(re.fullmatch(r'[0-9]+', value)) or
            (len(value) <= 256 and ('tausend' in value or 'hundert' in value)
             and _NUMBER_SEQUENCE.fullmatch(value) is not None))


def _under_thousand(value: str) -> int | None:
    if not value:
        return 0
    if value in _CARDINALS:
        return _CARDINALS[value]
    for prefix, hundreds in _HUNDREDS.items():
        if value.startswith(prefix):
            remainder = value[len(prefix):]
            if not remainder or remainder in _CARDINALS:
                return hundreds + (_CARDINALS[remainder] if remainder else 0)
    return None


def _spoken_year(value: str) -> int | None:
    """Only complete four-digit years, with at most one thousand/hundred group."""
    value = _compact(value)
    if len(value) > 96:
        return None
    if re.fullmatch(r'[0-9]{4}', value):
        number = int(value)
        return number if 1000 <= number <= 9999 else None
    for prefix, thousands in _THOUSANDS.items():
        if value.startswith(prefix):
            remainder = _under_thousand(value[len(prefix):])
            if remainder is not None:
                return thousands + remainder
    for prefix, hundreds in _YEAR_HUNDREDS.items():
        if value.startswith(prefix):
            remainder = value[len(prefix):]
            if not remainder or remainder in _CARDINALS:
                return hundreds + (_CARDINALS[remainder] if remainder else 0)
    return None


def _spoken_date(value: str) -> date | bool | None:
    """Return a date, False for recognized invalid/ambiguous dates, or None.

    Ordinary titles must not incur a full vocabulary scan. Invalid recognized
    dates must not fall through to ordinary text or a shorter date suffix.
    """
    value = _compact(value)
    if (not value or not (re.search(r'[0-9]|tausend|hundert', value)
                          or any(month in value for month in _MONTHS))):
        return None
    candidates = set()
    recognized = False
    for month_name, month in _MONTHS.items():
        if month_name not in value:
            continue
        # All German month names occur at most once in a valid complete date.
        parts = value.split(month_name)
        if len(parts) != 2:
            continue
        day_text, year_text = parts
        numeric_day = re.fullmatch(r'[0-9]{1,2}\.?', day_text)
        if not (numeric_day or day_text in _ORDINALS or day_text in _INVALID_ORDINALS):
            continue
        recognized = True
        day = (_ORDINALS.get(day_text) if not numeric_day
               else int(day_text.rstrip('.')))
        year = _spoken_year(year_text)
        if day is not None and year is not None:
            try:
                candidates.add(date(year, month, day))
            except ValueError:
                pass
    if recognized:
        return next(iter(candidates)) if len(candidates) == 1 else False
    if len(value) > 160:
        return False if _year_like(value) else None
    # Only actually matching number suffixes are considered, including invalid
    # month/day values. No 12-by-31 product scan and no digit concatenation.
    for day_text, day in _SPOKEN_NUMBERS.items():
        if not value.endswith(day_text):
            continue
        without_day = value[:-len(day_text)]
        if _year_like(without_day):
            recognized = True  # Missing one complete month/day group.
        for month_text, month in _SPOKEN_NUMBERS.items():
            if not without_day.endswith(month_text):
                continue
            year_text = without_day[:-len(month_text)]
            if not _year_like(year_text):
                continue
            recognized = True
            year = _spoken_year(year_text)
            if year is not None:
                try:
                    candidates.add(date(year, month, day))
                except ValueError:
                    pass
    return next(iter(candidates)) if len(candidates) == 1 else (False if recognized else None)


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
    # The earliest valid suffix consumes the complete longest spoken date.
    # Never retry a shorter suffix based on whether a page title happens to match.
    if not (re.search(r'[0-9]{4}|tausend|hundert', value)
            or any(month in value for month in _MONTHS)):
        return ('text', ''.join(value.split()))
    starts = [0] + [match.end() for match in re.finditer(r'\s+', value)]
    for start in starts:
        spoken = _spoken_date(value[start:])
        if spoken is False:
            return None
        if spoken is not None:
            return ('date', ''.join(value[:start].split()), spoken)
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
