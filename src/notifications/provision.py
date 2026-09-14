"""Provision VAPID secrets in the PRIVATE queue repo, never in the public tree.

Keeps an encrypted DPAPI copy outside Git for the optional Windows fast sender.
Reruns reuse this key; a missing local key plus existing remote secret is an
error, never an implicit rotation that would break existing phone subscriptions.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from src.bridge.github import GitHub
from src.notifications.pc import protected_bytes

QUEUE = "jdyece25-byte/schedule-requests"
TARGET = "jdyece25-byte/schedule"
SITE = "https://jdyece25-byte.github.io/schedule/"


def gh(*arguments, data=None):
    result = subprocess.run(["gh", *arguments], input=data, capture_output=True,
                            timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("GitHub secret provisioning failed; no credentials were printed")
    return result.stdout


def main():
    github = GitHub()
    if github.api("repos/" + QUEUE).get("private") is not True:
        raise RuntimeError("Subscription storage must be a private repository")
    root = Path(os.environ["LOCALAPPDATA"]) / "SchedulePush"
    root.mkdir(parents=True, exist_ok=True)
    protected = root / "vapid.dpapi"
    names = json.loads(gh("secret", "list", "--repo", QUEUE, "--json", "name"))
    if protected.exists():
        pem = protected_bytes(protected.read_bytes(), decrypt=True)
        key = serialization.load_pem_private_key(pem, password=None)
    else:
        if any(item["name"] == "VAPID_PRIVATE_KEY" for item in names):
            raise RuntimeError("An existing VAPID key must be recovered before provisioning; it was not rotated")
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        # Exclusive creation also prevents parallel provisioning from silently
        # replacing a previously generated key.
        with protected.open("xb") as file:
            file.write(protected_bytes(pem))
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise RuntimeError("VAPID requires a P-256 private key")
    public = base64.urlsafe_b64encode(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode("ascii")
    for name, value in (("VAPID_PRIVATE_KEY", pem), ("VAPID_SUBJECT", SITE.encode("ascii"))):
        gh("secret", "set", name, "--repo", QUEUE, data=value)
    public_config = {"version": 1, "vapidPublicKey": public, "queueRepo": QUEUE,
                     "targetRepo": TARGET, "siteUrl": SITE}
    source = Path(__file__).resolve().parents[1]
    (source / "push-config.json").write_text(json.dumps(public_config, indent=2) + "\n", encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"queue_repo": QUEUE, "target_repo": TARGET,
                                                  "subject": SITE}, indent=2) + "\n", encoding="utf-8")
    print("Private repository secrets configured; only the public VAPID key was written to src/push-config.json.")
    print("The optional PC key is protected with Windows DPAPI outside the repository.")


if __name__ == "__main__":
    main()
