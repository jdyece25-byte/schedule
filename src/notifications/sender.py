"""Send encrypted Web Push using a CAS lease in the PRIVATE request repository.

Run from Actions or the independent PC push helper. This never touches the DB,
worker service state, or local Git checkout. Logs contain counts/status only.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.bridge.github import GitHub, GitHubError
from src.notifications.scheduler import (KST, cancelled, changes, digest, display, eligible, notification,
                                         order, parse_stamp, scheduled, school_notices, snapshot, stamp)

STATE_PATH = "notification-state/state.json"
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com"}
LEASE_SECONDS = 180


class Busy(RuntimeError):
    pass


def validate_subscription(value, device_id):
    """Do not permit attacker-controlled URLs to turn the sender into an SSRF relay."""
    if not DEVICE_RE.fullmatch(device_id) or not isinstance(value, dict):
        return False
    if value.get("version") != 1 or value.get("device_id") != device_id or value.get("timezone") != "Asia/Seoul":
        return False
    try:
        sub = value["subscription"]
        endpoint = sub["endpoint"]
        if not isinstance(endpoint, str) or len(endpoint) > 4096:
            return False
        url = urlsplit(endpoint)
        hostname = url.hostname or ""
        allowed = hostname in PUSH_HOSTS or (hostname.endswith(".push.apple.com")
                                             and hostname != ".push.apple.com")
        if not allowed or url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.fragment:
            return False
        for name, length in (("p256dh", 65), ("auth", 16)):
            encoded = sub["keys"][name]
            if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
                return False
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(raw) != length or (name == "p256dh" and raw[0] != 4):
                return False
        parse_stamp(value["created_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


class WebPushTransport:
    def __init__(self, private_key, subject):
        from py_vapid import Vapid
        import requests
        if not private_key:
            raise ValueError("VAPID_PRIVATE_KEY is required")
        contact = urlsplit(subject)
        if (contact.scheme != "https" or not contact.hostname or contact.username or contact.password
                or contact.port not in (None, 443) or contact.query or contact.fragment
                or any(character.isspace() for character in subject)):
            raise ValueError("VAPID_SUBJECT must be an HTTPS contact URL without credentials")
        self.key = (Vapid.from_pem(private_key.encode()) if "-----BEGIN" in private_key
                    else Vapid.from_string(private_key.strip()))
        # This contact identifier need not be the application's landing path.
        # Keep py-vapid's strict checks enabled: its HTTPS regex accepts origins
        # but rejects a path. The validated site's origin is a sufficient contact.
        self.subject = "https://" + contact.hostname

        class NoRedirectSession(requests.Session):
            def request(self, method, url, **kwargs):
                # No capability URL or VAPID authorization may follow redirects.
                kwargs["allow_redirects"] = False
                return super().request(method, url, **kwargs)

        self.session = NoRedirectSession()
        self.session.trust_env = False

    def send(self, subscription, notice, now):
        from pywebpush import webpush, WebPushException
        ttl = min(3600, max(1, int((parse_stamp(notice["expires"]) - now).total_seconds())))
        try:
            response = webpush(subscription_info=subscription["subscription"],
                               data=json.dumps(notice["payload"], ensure_ascii=False),
                               vapid_private_key=self.key, vapid_claims={"sub": self.subject},
                               timeout=30, ttl=ttl, requests_session=self.session,
                               headers={"Urgency": "high", "Topic": digest(notice["id"])[:32]})
            return int(response.status_code)
        except WebPushException as error:
            # WebPushException text includes endpoint/body. Never print it.
            return int(error.response.status_code) if error.response is not None else 0
        except Exception:
            return 0


def empty_state():
    return {"version": 1, "snapshot": None, "outbox": [], "delivered": {}, "lease": None}


class StateStore:
    def __init__(self, github, queue, now):
        self.github, self.queue, self.now = github, queue, now
        self.owner = uuid.uuid4().hex
        self.state = None
        self.sha = None

    def read(self):
        value, sha = self.github.read_json(self.queue, STATE_PATH)
        value = empty_state() if value is None else value
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("outbox"), list)
                or not isinstance(value.get("delivered"), dict)
                or (value.get("snapshot") is not None and not isinstance(value["snapshot"], dict))):
            raise ValueError("Private notification state has an unsupported format")
        return value, sha

    def acquire(self):
        self.state, self.sha = self.read()
        lease = self.state.get("lease")
        if lease and parse_stamp(lease["until"]) > self.now():
            raise Busy("Another sender is active")
        self.state["lease"] = {"owner": self.owner, "until": stamp(self.now() + timedelta(seconds=LEASE_SECONDS))}
        try:
            self.sha = self.github.put_json(self.queue, STATE_PATH, self.state, self.sha,
                                            message="Acquire private notification delivery lease")
        except GitHubError as error:
            if error.status in (409, 422):
                raise Busy("Another sender acquired the lease") from None
            raise

    def save(self, release=False):
        self.state["lease"] = None if release else {
            "owner": self.owner, "until": stamp(self.now() + timedelta(seconds=LEASE_SECONDS))}
        for attempt in range(3):
            try:
                self.sha = self.github.put_json(self.queue, STATE_PATH, self.state, self.sha,
                                                message="Update private notification delivery state")
                return
            except GitHubError as error:
                if error.status not in (409, 422):
                    raise
                current, current_sha = self.read()
                # A simultaneous queue commit may cause a transient ref conflict.
                # Retry only if nobody changed this state blob/ownership.
                if current_sha != self.sha or (current.get("lease") or {}).get("owner") != self.owner:
                    raise Busy("Notification lease ownership changed") from None
        raise Busy("Private repository is busy")


def load_subscriptions(github, queue):
    revision = github.head(queue)
    paths = sorted(path for path in github.tree(queue, revision)
                   if re.fullmatch(r"subscriptions/[A-Za-z0-9_-]{1,100}\.json", path))
    result = []
    for path in paths:
        try:
            value, _ = github.read_json(queue, path, revision)
            if validate_subscription(value, Path(path).stem):
                result.append(value)
        except (ValueError, TypeError):
            continue  # Invalid device records cannot break every other device.
    return result


def load_school_notices(github, queue, now):
    try:
        revision = github.head(queue)
        index, _ = github.read_json(queue, "school/index.json", revision)
        tree = github.tree(queue, revision)
        pending = []
        for path in tree:
            if not re.fullmatch(r"school/decisions/[A-Za-z0-9_-]{1,100}\.json", path):
                continue
            if path.replace("school/decisions/", "school/decision-results/", 1) in tree:
                continue  # Terminal results and index.review are committed together.
            decision, _ = github.read_json(queue, path, revision)
            if isinstance(decision, dict) and decision.get("id") == Path(path).stem:
                pending.append(decision)
        return school_notices(index, now, pending)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        # A collector error or absent/private malformed index cannot block the
        # independently useful daily, deadline, change and departure reminders.
        return []


def test_notice(subscription, events, travel, now):
    try:
        due = parse_stamp(subscription["test_requested_at"])
        if not due <= now < due + timedelta(minutes=15):
            return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    future = sorted((e for e in events if e["d"] >= now.astimezone(KST).date().isoformat()
                     and not cancelled(e)), key=order)
    notice = notification("test", "test:" + subscription["device_id"] + ":" + stamp(due),
                          due, due + timedelta(minutes=15), [display(e, travel) for e in future[:1]])
    notice["payload"]["kind"] = "daily"
    return notice


def due_for_device(subscription, notice, now):
    if notice["kind"] == "test":
        return subscription.get("enabled") is True and parse_stamp(notice["due"]) <= now < parse_stamp(notice["expires"])
    return eligible(subscription, notice, now)


def delivery_key(subscription, notice):
    return digest([subscription["device_id"], notice["id"]])


def disable_expired(github, queue, subscription, now):
    path = "subscriptions/" + subscription["device_id"] + ".json"
    latest, sha = github.read_json(queue, path)
    # A phone may have refreshed its subscription while the request was in flight.
    if latest and latest.get("subscription") == subscription.get("subscription") and latest.get("enabled") is True:
        latest.update(enabled=False, updated_at=stamp(now), disabled_reason="subscription_expired")
        try:
            github.put_json(queue, path, latest, sha, message="Disable expired push subscription")
        except GitHubError as error:
            if error.status not in (409, 422):
                raise


def run(github, transport, queue, target, changes_only=False, now=None):
    now = now or (lambda: datetime.now(timezone.utc))
    started = now()
    if queue == target or not github.api("repos/" + queue).get("private"):
        raise ValueError("Notification subscriptions/state require a PRIVATE repository")
    revision = github.head(target)
    events, _ = github.read_json(target, "DB/events.json", revision)
    travel, _ = github.read_json(target, "DB/travel.json", revision)
    if not isinstance(events, list) or not isinstance(travel, dict):
        raise ValueError("Schedule DB is unavailable")
    current = snapshot(events, travel)
    subscribers = load_subscriptions(github, queue)
    store = StateStore(github, queue, now)
    old, _ = store.read()
    clock_notices = [] if changes_only else scheduled(events, travel, started)
    announcement_notices = load_school_notices(github, queue, started)

    def candidates(state, subscription):
        result = state["outbox"] + clock_notices + announcement_notices
        test = test_notice(subscription, events, travel, started)
        return result + ([test] if test else [])

    has_work = old["snapshot"] != current or any(
        due_for_device(sub, notice, started) and delivery_key(sub, notice) not in old["delivered"]
        for sub in subscribers for notice in candidates(old, sub))
    if not has_work:
        return {"status": "idle", "sent": 0, "retry": 0, "expired": 0}
    store.acquire()
    counts = {"status": "ok", "sent": 0, "retry": 0, "expired": 0}
    try:
        state = store.state
        # A reviewer may have applied/ignored a notice while we waited for the
        # shared send lease. Consume the latest private state before delivery.
        announcement_notices = load_school_notices(github, queue, now())
        # A waiting runner may have read an older DB revision than the sender
        # that just released the lease. Refresh after acquisition before diffing.
        latest_revision = github.head(target)
        if latest_revision != revision:
            revision = latest_revision
            events, _ = github.read_json(target, "DB/events.json", revision)
            travel, _ = github.read_json(target, "DB/travel.json", revision)
            current = snapshot(events, travel)
            clock_notices = [] if changes_only else scheduled(events, travel, started)
        if state["snapshot"] != current:
            # Commit time gates new subscriptions, rather than the later cron time.
            metadata = github.api(f"repos/{target}/git/commits/{revision}")
            changed_at = min(started, parse_stamp(metadata["committer"]["date"]))
            state["outbox"].extend(changes(state["snapshot"], current, revision, changed_at, started))
            state["snapshot"] = current
            state["revision"] = revision
        state["outbox"] = [n for n in state["outbox"] if parse_stamp(n["expires"]) > started]
        state["delivered"] = {key: value for key, value in state["delivered"].items()
                              if parse_stamp(value) > started - timedelta(days=14)}
        # Durably write the outbox before any network sends. A crash keeps retries.
        store.save()
        for initial_subscription in subscribers:
            # Honor opt-out or refreshed browser keys written during this run.
            device_id = initial_subscription["device_id"]
            subscription, _ = github.read_json(queue, f"subscriptions/{device_id}.json")
            if not validate_subscription(subscription, device_id):
                continue
            for notice in candidates(state, subscription):
                moment = now()
                key = delivery_key(subscription, notice)
                if key in state["delivered"] or not due_for_device(subscription, notice, moment):
                    continue
                # Renew the shared lease before each bounded network operation.
                if parse_stamp(state["lease"]["until"]) < moment + timedelta(seconds=70):
                    store.save()
                status = transport.send(subscription, notice, moment)
                if status in (200, 201, 202):
                    state["delivered"][key] = stamp(now())
                    counts["sent"] += 1
                    store.save()
                elif status in (404, 410):
                    disable_expired(github, queue, subscription, now())
                    counts["expired"] += 1
                    break
                else:
                    # 429/5xx/timeouts/authentication errors are not delivery.
                    # A future cron/PC pass retries; never log provider response text.
                    counts["retry"] += 1
                    break
        state["last_run_at"] = stamp(now())
        state["last_run"] = {key: counts[key] for key in ("sent", "retry", "expired")}
        return counts
    finally:
        store.save(release=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private-repository schedule Web Push sender")
    parser.add_argument("--queue-repo", default="jdyece25-byte/schedule-requests")
    parser.add_argument("--target-repo", default="jdyece25-byte/schedule")
    parser.add_argument("--changes-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not REPO_RE.fullmatch(args.queue_repo) or not REPO_RE.fullmatch(args.target_repo):
            raise ValueError("Invalid repository name")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        github = GitHub(token=token)  # PC helper may instead use gh's OS keyring.
        transport = WebPushTransport(os.environ.get("VAPID_PRIVATE_KEY", ""),
                                     os.environ.get("VAPID_SUBJECT", "https://jdyece25-byte.github.io/schedule/"))
        result = run(github, transport, args.queue_repo, args.target_repo, args.changes_only)
        print(json.dumps(result))
        return 1 if result["retry"] else 0
    except Busy:
        print('{"status":"busy","retry":1}')
        return 75
    except Exception as error:
        # No exception text: upstream exceptions may contain subscription secrets.
        print(json.dumps({"status": "error", "type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
