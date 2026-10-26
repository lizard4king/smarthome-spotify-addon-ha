"""Bounded, one-shot delivery of an existing Lumi job to its paired Echo."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

import aiohttp

SPEECH_HOST = "7b071411-smarthome-cockpit"
SPEECH_PORT = 8767
SPEECH_PATH = "/api/codex/speech"
ROOMS = {"buero", "schlafzimmer", "kueche", "wohnzimmer", "badezimmer"}
MAX_DELIVERIES = 32
MAX_ATTEMPTS = 1024
POLL_SECONDS = 0.5
FAILURE = "Der Gesprächsdienst ist gerade nicht erreichbar."
LOG = logging.getLogger("music_alexa.answer_delivery")


def configuration(settings):
    raw = getattr(settings, "lumi_answer_targets_json", "")
    secret = getattr(settings, "lumi_answer_bridge_secret", "")
    if not raw and not secret:
        return {}, ""
    if not isinstance(raw, str) or not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise ValueError("Invalid answer delivery configuration")
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate answer target")
            result[key] = value
        return result
    targets = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(targets, dict) or len(targets) > 256 or any(
            not isinstance(device, str) or not re.fullmatch(r"amzn1\.ask\.device\.[A-Za-z0-9._-]{1,1000}", device)
            or not isinstance(room, str) or room not in ROOMS for device, room in targets.items())):
        raise ValueError("Invalid answer targets")
    return targets, secret


def _mac(secret, *parts):
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def bounded_speech(text):
    if not isinstance(text, str):
        raise ValueError("Invalid speech page")
    text = " ".join(text.split())
    if not text:
        raise ValueError("Empty answer")
    if len(text) <= 2000:
        return text
    # The relay owns full answers and page advancement. Never silently discard
    # the remaining answer at this final, device-facing boundary.
    raise ValueError("Speech page exceeds limit")


class SpeechClient:
    def __init__(self, session, secret, *, port=SPEECH_PORT):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
            raise ValueError("Invalid answer secret")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid speech port")
        self.session, self.secret = session, secret
        self.origin = f"http://{SPEECH_HOST}:{port}"

    async def speak(self, request_id, target, text):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id) or target not in ROOMS:
            raise ValueError("Invalid speech request")
        body = json.dumps({"request_id": request_id, "target": target, "text": bounded_speech(text)},
                          separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
        nonce, stamp = secrets.token_hex(16), str(int(time.time()))
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "X-Control-Timestamp": stamp, "X-Control-Nonce": nonce,
                   "X-Control-Signature": _mac(self.secret, "POST", SPEECH_PATH, stamp, nonce, hashlib.sha256(body).hexdigest())}
        # Never retry an uncertain HTTP outcome: HA may already have spoken it.
        async with self.session.post(self.origin + SPEECH_PATH, data=body, headers=headers,
                                     allow_redirects=False, timeout=aiohttp.ClientTimeout(total=12)) as response:
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(8192):
                size += len(chunk)
                if size > 16384:
                    raise ValueError("Speech response too large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if (len(raw) > 16384 or str(response.url) != self.origin + SPEECH_PATH
                    or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
                    or response.headers.get("Content-Encoding") or response.headers.get("Transfer-Encoding")
                    or any(len(response.headers.getall(key, [])) != 1 for key in ("Content-Type", "X-Control-Signature"))):
                raise ValueError("Invalid speech response")
            expected = _mac(self.secret, "RESPONSE", SPEECH_PATH, nonce, str(response.status), hashlib.sha256(raw).hexdigest())
            if not hmac.compare_digest(expected, response.headers.get("X-Control-Signature", "")):
                raise ValueError("Unauthenticated speech response")
            value = json.loads(raw)
            if (response.status != 200 or not isinstance(value, dict) or set(value) != {"receipt"}
                    or not isinstance(value["receipt"], str) or not 1 <= len(value["receipt"]) <= 2000):
                raise ValueError("Speech failed")


@dataclass(eq=False)
class Delivery:
    principal: str
    device: str
    user: str
    mapping: dict
    target: str | None
    secret: str
    request_id: str
    job: object = None
    task: asyncio.Task | None = None
    accepted: bool = False


class AnswerDelivery:
    def __init__(self, session, settings, mappings, poll, *, port=SPEECH_PORT, finish=None):
        configuration(settings)
        self.session, self.settings, self.mappings, self.poll = session, settings, mappings, poll
        self.port, self.slots, self.closed = port, {}, False
        self.attempted = {}
        self.finish = finish
        self.tasks = set()

    def cancel(self, device):
        slot = self.slots.get(device)
        if slot:
            self.release(slot)
        if slot and slot.task:
            slot.task.cancel()
        return slot

    def reserve(self, principal, device, user):
        self.cancel(device)
        if self.closed or len(self.slots) >= MAX_DELIVERIES:
            return None
        targets, secret = configuration(self.settings)
        slot = Delivery(principal, device, user, dict(self.mappings().get(device, {})),
                        targets.get(device), secret, secrets.token_hex(16))
        self.slots[device] = slot
        return slot

    def current(self, slot):
        if self.slots.get(slot.device) is not slot or self.closed:
            return False
        targets, secret = configuration(self.settings)
        mapping = self.mappings().get(slot.device)
        return (mapping == slot.mapping and mapping is not None
                and hmac.compare_digest(mapping.get("user_id", ""), slot.user)
                and targets.get(slot.device) == slot.target and hmac.compare_digest(secret, slot.secret))

    def release(self, slot):
        if self.slots.get(slot.device) is slot:
            self.slots.pop(slot.device, None)
            if self.finish:
                self.finish(slot)

    def start(self, slot):
        if not slot.target or slot.job is None or not self.current(slot):
            self.release(slot)
            return False
        slot.task = asyncio.create_task(self._run(slot))
        self.tasks.add(slot.task)
        slot.task.add_done_callback(self.tasks.discard)
        return True

    async def _run(self, slot):
        failures = 0
        try:
            while self.current(slot) and time.monotonic() < slot.job.expires:
                await asyncio.sleep(POLL_SECONDS)
                if not self.current(slot):
                    return
                try:
                    text = await self.poll(slot.principal, slot.job)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    if failures < 3:
                        continue
                    text = FAILURE
                if text is None:
                    continue
                if self.current(slot) and time.monotonic() < slot.job.expires:
                    now = time.monotonic()
                    self.attempted = {key: expires for key, expires in self.attempted.items() if expires > now}
                    attempt_key = (slot.principal, slot.job.job_id)
                    if attempt_key in self.attempted or len(self.attempted) >= MAX_ATTEMPTS:
                        return
                    # Commit the attempt before I/O, including uncertain outcomes.
                    self.attempted[attempt_key] = slot.job.expires
                    await SpeechClient(self.session, slot.secret, port=self.port).speak(slot.request_id, slot.target, text)
                    slot.accepted = True
                    LOG.info("Lumi answer delivery accepted")
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # No secret/request content in logs; especially no delivery retry.
            LOG.warning("Lumi answer delivery failed")
        finally:
            self.release(slot)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        for slot in list(self.slots.values()):
            self.release(slot)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
