from dataclasses import dataclass


@dataclass(frozen=True)
class BankProfile:
    name: str
    endpoint: str | None
    source: str
    evidence: str


PROFILES = {
    'ING': BankProfile('ING', 'https://fints.ing.de/fints/', 'https://www.ing.de/hbci',
                       'Giro, Extra-Konto und Depot dokumentiert; tatsächlicher Abruf ungeprüft.'),
    'NASPA': BankProfile('NASPA', 'https://banking-hs7.s-fints-pt-hs.de/fints30',
                         'FinTS-Leitstelle: Bankenliste, erhalten 2026-10-05',
                         'PIN/TAN-Endpunkt in registrierter Bankenliste bestätigt; tatsächlicher Abruf ungeprüft.'),
    'POSTBANK': BankProfile('Postbank', 'https://hbci.postbank.de/banking/hbci.do',
                            'FinTS-Leitstelle: Bankenliste, erhalten 2026-10-05',
                            'PIN/TAN-Endpunkt bestätigt; Kreditkartenabruf mit dieser Bibliothek und diesem Konto ungeprüft; CSV bleibt Alternative.'),
}


def readiness():
    """Offline metadata only. Never constructs a client or reads credentials."""
    return {key: {'bank': value.name, 'endpoint_verified': value.endpoint is not None,
                  'live_tested': False, 'evidence': value.evidence,
                  'requirements': ['registrierte FinTS-Produktkennung', 'lokale interaktive Anmeldung',
                                   'geprüfter Bankendpunkt', 'bestätigte Kontozuordnung']}
            for key, value in PROFILES.items()}
