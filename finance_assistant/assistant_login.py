"""Owner-initiated device login; codes stay in memory and never enter logs."""
import re
import subprocess
import threading
import time

from .assistant_runtime import CODEX, _env

_lock = threading.Lock()
_process = None
_text = ''
_started = 0.0
_finished = False


def _expire(process):
    with _lock:
        if _process is not process or process.poll() is not None:
            return
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _collect(process):
    global _text, _finished
    try:
        while process.poll() is None:
            chunk = process.stdout.read(1)
            if not chunk:
                break
            with _lock:
                if _process is not process:
                    return
                _text += chunk.decode('utf-8', errors='replace')
                if len(_text) > 4096:
                    process.terminate()
                    return
    finally:
        with _lock:
            if _process is process:
                _finished = True


def start():
    global _process, _text, _started, _finished
    with _lock:
        if _process is not None and _process.poll() is None:
            return {'state': 'pending'}
        _text, _finished, _started = '', False, time.monotonic()
        _process = subprocess.Popen([CODEX, 'login', '--device-auth'], env=_env(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        expiry = threading.Timer(900, _expire, args=(_process,))
        expiry.daemon = True
        expiry.start()
        threading.Thread(target=_collect, args=(_process,), daemon=True).start()
    return {'state': 'pending'}


def state():
    with _lock:
        if _process is None:
            return {'state': 'idle'}
        if time.monotonic() - _started > 900:
            if _process.poll() is None:
                _process.terminate()
            return {'state': 'expired'}
        if _process.poll() == 0:
            return {'state': 'completed'}
        if _finished:
            return {'state': 'failed'}
        clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', _text)
        code = re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b', clean)
        urls = re.findall(r'https://[^\s<>"\']+', clean)
        if 'https://auth.openai.com/codex/device' in urls and code:
            return {'state': 'pending', 'url': 'https://auth.openai.com/codex/device',
                    'code': code.group()}
        return {'state': 'pending'}
