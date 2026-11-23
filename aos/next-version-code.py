#!/usr/bin/env python3
"""Print the next Google Play versionCode = (highest active across all tracks) + 1.

Reuses the authenticated client in scripts/store_release.py. Reads RELEASE_CONFIG
(or .private/release-config.json, fallback handoff/credentials/release-config.json).
Used by aos/auto-upload.sh so a release never collides with an existing versionCode.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.store_release import GoogleClient, load_config, _google_package_path  # noqa: E402


def highest_version_code() -> int:
    cfg = load_config()
    google = GoogleClient(cfg)
    package_path = _google_package_path(cfg["bundleId"])
    highest = 0
    for track in ("internal", "alpha", "beta", "production"):
        body = google.request("GET", package_path + f"/tracks/{track}/releases")
        for release in body.get("releases", []) or []:
            if not isinstance(release, dict):
                continue
            for artifact in release.get("activeArtifacts", []) or []:
                code = isinstance(artifact, dict) and artifact.get("versionCode")
                if isinstance(code, (int, str)) and str(code).isdigit():
                    highest = max(highest, int(code))
    return highest


if __name__ == "__main__":
    print(highest_version_code() + 1)
