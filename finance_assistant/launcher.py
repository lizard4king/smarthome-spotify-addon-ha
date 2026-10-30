"""Dedicated unprivileged inference worker; no host or financial database mounts."""
import os
from pathlib import Path
import resource
import signal
import subprocess


def main():
    os.umask(0o077)
    for directory in ('/data/home', '/data/codex', '/data/models'):
        path = Path(directory)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(path, 10001, 10001)
    # The startup secret is read once, before privileges are dropped.
    from server import read_token, Handler
    from http.server import ThreadingHTTPServer
    token = read_token()
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
    os.nice(10)
    if hasattr(os, 'sched_setaffinity'):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Inference has a hard 4-GiB address-space ceiling; HA remains separate.
    def model_limits():
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    # Start before the small file limit below is inherited by Codex processes.
    # Model downloads need larger files; a separate process owns only model storage.
    model = subprocess.Popen(['/usr/local/bin/ollama', 'serve'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             preexec_fn=model_limits)
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**2, 4 * 1024**2))
    try:
        server = ThreadingHTTPServer(('0.0.0.0', 8786), Handler)
        server.assistant_token = token
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        model.terminate()
        try:
            model.wait(timeout=10)
        except subprocess.TimeoutExpired:
            model.kill()
            model.wait()


if __name__ == '__main__':
    main()
