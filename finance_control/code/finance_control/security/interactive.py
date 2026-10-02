import getpass
import sys


def respond_to_challenge(challenge):
    """Local synchronous interaction only; no challenge/TAN persistence."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Lokales interaktives Terminal erforderlich.')
    # Suppress terminal control characters in untrusted bank text.
    text = ''.join(char for char in challenge.text if char.isprintable() or char == '\n')
    print(text)
    if challenge.decoupled:
        return input('In der Bank-App freigeben, danach mit JA bestätigen: ').strip() == 'JA'
    return getpass.getpass('TAN: ')
