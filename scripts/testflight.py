#!/usr/bin/env python3
"""Upload the the IPA and set up an internal TestFlight group + tester.

Prerequisite that cannot be automated: the the app record must already exist
in App Store Connect (create it once in the ASC UI). Apple's public API does
not allow creating an app record. Everything after that is handled here.

Steps:
  1. Resolve the app id from the project bundle id.
  2. Upload the IPA with altool (ASC API key in ~/.appstoreconnect/private_keys).
  3. Poll until the build finishes processing (VALID).
  4. Clear export-compliance if needed (Info.plist already sets
     ITSAppUsesNonExemptEncryption=false, so this is usually a no-op).
  5. Create / reuse an internal beta group with access to all builds.
  6. Add the tester email -> ASC sends the TestFlight invite mail.
"""
import os
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store_release import (  # noqa: E402
    AppleClient, ReleaseError, ApiError, load_config, project_package,
    _attr, _resource_id, _single_data,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IPA = ROOT / "release-artifacts" / "app-ios-release.ipa"
# The ASC account holder (only ASC user on team <YOUR_TEAM_ID>). Internal TestFlight
# testers must be ASC team members, so this is the eligible address.
DEFAULT_TESTER = os.environ.get("TESTFLIGHT_TESTER_EMAIL", "")


def ipa_identity(ipa: Path) -> tuple[str, str, str]:
    """Read the uploaded artifact's identity, never app.json's default build 1."""
    try:
        with zipfile.ZipFile(ipa) as archive:
            entries = [name for name in archive.namelist()
                       if len(name.split("/")) == 3
                       and name.startswith("Payload/")
                       and name.split("/")[1].endswith(".app")
                       and name.endswith("/Info.plist")]
            if len(entries) != 1:
                raise ReleaseError("IPA must contain exactly one application Info.plist")
            info = plistlib.loads(archive.read(entries[0]))
    except (OSError, zipfile.BadZipFile, plistlib.InvalidFileException, ValueError) as exc:
        raise ReleaseError(f"Cannot read IPA identity: {ipa}") from exc
    keys = ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion")
    if not isinstance(info, dict) or any(not isinstance(info.get(k), str) or not info[k] for k in keys):
        raise ReleaseError("IPA is missing its bundle ID, version, or build number")
    return tuple(info[k] for k in keys)


def resolve_app_id(client: AppleClient, bundle_id: str) -> str:
    for app in client.all("/v1/apps", params={"limit": "200"}):
        if _attr(app, "bundleId") == bundle_id:
            return _resource_id(app, "the app")
    raise ReleaseError(
        f"No App Store Connect app record for {bundle_id}. "
        "Create it once in the ASC UI (My Apps -> + -> New App), then re-run."
    )


def upload_ipa(ipa: Path, key_id: str, issuer_id: str) -> None:
    if not ipa.is_file():
        raise ReleaseError(f"IPA not found: {ipa}")
    print(f"Uploading {ipa.name} via altool ...", flush=True)
    proc = subprocess.run(
        ["xcrun", "altool", "--upload-app", "--type", "ios", "-f", str(ipa),
         "--apiKey", key_id, "--apiIssuer", issuer_id],
        capture_output=True, text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        if "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE" in out or "already been used" in out.lower():
            print("Build already uploaded; continuing to processing wait.", flush=True)
            return
        raise ReleaseError("altool upload failed:\n" + out[-1500:])
    print("Upload accepted by App Store Connect.", flush=True)


def wait_for_build(client: AppleClient, app_id: str, version: str, build_number: str,
                   timeout_s: int = 2400) -> dict:
    print(f"Waiting for build {version} ({build_number}) to process ...", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        builds = client.all("/v1/builds", params={
            "filter[app]": app_id, "limit": "50", "sort": "-uploadedDate",
            "filter[version]": build_number,
        })
        for b in builds:
            if _attr(b, "version") != build_number or _attr(b, "expired") is True:
                continue
            build_id = _resource_id(b, "build")
            pre_release = _single_data(client.request(
                "GET", f"/v1/builds/{build_id}/preReleaseVersion"), "build version")
            if _attr(pre_release, "version") != version or _attr(pre_release, "platform") != "IOS":
                continue
            state = _attr(b, "processingState")
            print(f"  build {build_number}: {state}", flush=True)
            if state == "VALID":
                return b
            if state in ("INVALID", "FAILED"):
                raise ReleaseError(f"Build processing ended in state {state}")
        time.sleep(30)
    raise ReleaseError("Timed out waiting for build to process")


def clear_export_compliance(client: AppleClient, build_id: str) -> None:
    try:
        detail = client.request("GET", f"/v1/builds/{build_id}/buildBetaDetail")
        internal = _attr(_single_data(detail, "buildBetaDetail"), "internalBuildState")
    except ReleaseError:
        internal = None
    if internal == "MISSING_EXPORT_COMPLIANCE":
        client.request("PATCH", f"/v1/builds/{build_id}", json_body={
            "data": {"type": "builds", "id": build_id,
                     "attributes": {"usesNonExemptEncryption": False}},
        })
        print("Cleared export compliance on build.", flush=True)


def ensure_internal_group(client: AppleClient, app_id: str, name: str) -> str:
    for g in client.all(f"/v1/apps/{app_id}/betaGroups", params={"limit": "200"}):
        if _attr(g, "isInternalGroup") and _attr(g, "name") == name:
            return _resource_id(g, "beta group")
    created = _single_data(client.request("POST", "/v1/betaGroups", json_body={
        "data": {"type": "betaGroups",
                 "attributes": {"name": name, "isInternalGroup": True, "hasAccessToAllBuilds": True},
                 "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}},
    }), "beta group")
    gid = _resource_id(created, "beta group")
    print(f"Created internal beta group '{name}'.", flush=True)
    return gid


def add_tester(client: AppleClient, group_id: str, email: str) -> None:
    members = client.all(f"/v1/betaGroups/{group_id}/betaTesters", params={"limit": "200"})
    if any(str(_attr(m, "email") or "").lower() == email.lower() for m in members):
        print(f"Tester {email} is already in the group.", flush=True)
        return
    try:
        client.request("POST", "/v1/betaTesters", json_body={
            "data": {"type": "betaTesters",
                     "attributes": {"email": email},
                     "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group_id}]}}},
        })
        print(f"Invited tester {email}. Invitation email sent.", flush=True)
    except ApiError as exc:
        # Already a tester: ensure group membership instead of failing.
        if "409" in str(exc) or "already" in str(exc).lower():
            client.request("POST", f"/v1/betaGroups/{group_id}/relationships/betaTesters", json_body={
                "data": [{"type": "betaTesters", "id": _tester_id(client, email)}],
            })
            print(f"Tester {email} already existed; ensured group membership.", flush=True)
        else:
            raise


def _tester_id(client: AppleClient, email: str) -> str:
    testers = client.all("/v1/betaTesters", params={"filter[email]": email, "limit": "1"})
    if not testers:
        raise ReleaseError(f"Could not resolve existing tester {email}")
    return _resource_id(testers[0], "beta tester")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipa", default=str(DEFAULT_IPA))
    parser.add_argument("--version", help="Expected version (defaults to the IPA's version)")
    parser.add_argument("--build-number", help="Expected build number (defaults to the IPA's build)")
    parser.add_argument("--tester", default=DEFAULT_TESTER)
    parser.add_argument("--group", default="Internal Testers")
    parser.add_argument("--skip-upload", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        bundle_id = project_package("ios")
        ipa = Path(args.ipa)
        ipa_bundle_id, version, build_number = ipa_identity(ipa)
        if ipa_bundle_id != bundle_id:
            raise ReleaseError("IPA bundle ID does not match the project")
        if args.version is not None and args.version != version:
            raise ReleaseError("Requested version does not match the IPA")
        if args.build_number is not None and args.build_number != build_number:
            raise ReleaseError("Requested build number does not match the IPA")
        apple = config["apple"]
        client = AppleClient(config)
        app_id = resolve_app_id(client, bundle_id)
        print(f"App record: {app_id} ({bundle_id})", flush=True)
        if not args.skip_upload:
            upload_ipa(ipa, apple["keyId"], apple["issuerId"])
        build = wait_for_build(client, app_id, version, build_number)
        build_id = _resource_id(build, "build")
        clear_export_compliance(client, build_id)
        group_id = ensure_internal_group(client, app_id, args.group)
        add_tester(client, group_id, args.tester)
        print(f"\nTestFlight ready. Build {version} ({build_number}) is in "
              f"group '{args.group}'. {args.tester} received an invite.", flush=True)
        return 0
    except ReleaseError as exc:
        print(f"TestFlight error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
