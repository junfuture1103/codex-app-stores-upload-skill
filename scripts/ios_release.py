#!/usr/bin/env python3
"""iOS release: inspect, plan, build/upload, and submit with consumables.

status/plan are read-only. release uploads a signed IPA; --submit also sends it
to review. submit resumes from a processed build. No Google operations or
TestFlight invitations are performed. Credentials/reviewer notes are not logged.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

from store_release import (AppleClient, ReleaseError, ROOT, _attr, _single_data,
                           load_config, readiness_report, require_project_identity,
                           require_ready)
from testflight import ipa_identity, resolve_app_id, wait_for_build

PRODUCTS = ("starlight_50", "starlight_100")
EDITABLE = {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "DEVELOPER_REJECTED",
            "REJECTED", "METADATA_REJECTED"}
ACTIVE_REVIEW = {"WAITING_FOR_REVIEW", "IN_REVIEW", "CANCELING", "COMPLETING"}
RELATIONS = "appStoreVersion,inAppPurchaseVersion"
LOGIN_REVIEW_EVIDENCE = ("rejectionSource", "signedBuildIdentity", "reviewerApiLogin", "releaseSimulatorLogin")
FEATURE_REVIEW_EVIDENCE = ("signedBuildIdentity", "automatedTests", "nativeAppFlow", "productionApi", "subscriptionTerms")


def get(client, path):
    return _single_data(client.request("GET", path), path)


def state(item):
    return _attr(item, "appVersionState") or _attr(item, "appStoreState") or _attr(item, "state")


def relation(item, name):
    return item.get("relationships", {}).get(name, {}).get("data")


def snapshot(client, config, *, allow_missing_version=False):
    bundle = require_project_identity(config, "ios")
    app_id = str(config["apple"].get("appId") or resolve_app_id(client, bundle))
    if not app_id.isdigit() or _attr(get(client, f"/v1/apps/{app_id}"), "bundleId") != bundle:
        raise ReleaseError("Apple app does not match this project's bundle ID")
    version = json.loads((ROOT / "app.json").read_text())["expo"]["version"]
    versions = client.all(f"/v1/apps/{app_id}/appStoreVersions", {"limit": "200"})
    matches = [v for v in versions if _attr(v, "platform") == "IOS" and _attr(v, "versionString") == version]
    if len(matches) != 1 and not (allow_missing_version and not matches):
        raise ReleaseError("Create exactly one matching iOS version in App Store Connect first")
    selected = matches[0] if matches else None
    builds = client.all("/v1/builds", {"filter[app]": app_id, "limit": "200"})
    uploads = client.all(f"/v1/apps/{app_id}/buildUploads", {"limit": "200"})
    used = {_attr(b, "version") for b in builds}
    used.update(_attr(b, "cfBundleVersion") for b in uploads if _attr(b, "platform") == "IOS")
    # This pipeline uses integer build numbers. Do not silently misorder dotted builds.
    if any(not str(n).isdigit() for n in used if n is not None):
        raise ReleaseError("Non-integer remote build number; choose a numbering scheme explicitly")
    latest = max((int(n) for n in used if n is not None), default=0)
    submissions = client.all(f"/v1/apps/{app_id}/reviewSubmissions", {"limit": "200"})
    active = []
    for sub in submissions:
        if _attr(sub, "platform") not in (None, "IOS") or state(sub) in {"COMPLETE", "CANCELED"}:
            continue
        sub = dict(sub)
        sub["items"] = client.all(f"/v1/reviewSubmissions/{sub['id']}/items",
                                  {"limit": "200", "include": RELATIONS})
        active.append(sub)
    products = []
    available = client.all(f"/v1/apps/{app_id}/inAppPurchasesV2", {"limit": "200"})
    for product_id in PRODUCTS:
        found = [p for p in available if _attr(p, "productId") == product_id]
        if len(found) != 1 or _attr(found[0], "inAppPurchaseType") != "CONSUMABLE":
            raise ReleaseError(f"Expected one consumable product: {product_id}")
        product = found[0]
        versions = client.all(f"/v2/inAppPurchases/{product['id']}/versions", {"limit": "200"})
        pending = [v for v in versions if state(v) not in {"APPROVED", "REPLACED_WITH_NEW_VERSION"}]
        if state(product) == "APPROVED":
            target = None
        elif len(pending) == 1:
            target = pending[0]
        else:
            raise ReleaseError(f"Expected one reviewable version for {product_id}")
        products.append({"productId": product_id, "id": product["id"], "state": state(product),
                         "versionId": target["id"] if target else None,
                         "versionState": state(target) if target else "APPROVED"})
    attached = client.request("GET", f"/v1/appStoreVersions/{selected['id']}/build").get("data") if selected else None
    return {"appId": app_id, "bundleId": bundle, "version": version, "appVersion": selected,
            "builds": builds, "latestBuildNumber": latest, "nextBuildNumber": latest + 1,
            "attachedBuild": attached, "submissions": active, "submissionHistory": submissions,
            "products": products}


def targets_for(snap):
    return [("appStoreVersion", {"type": "appStoreVersions", "id": snap["appVersion"]["id"]})] + [
        ("inAppPurchaseVersion", {"type": "inAppPurchaseVersions", "id": p["versionId"]})
        for p in snap["products"] if p["versionId"]]


def item_target(item):
    values = [(k, relation(item, k)) for k in ("appStoreVersion", "inAppPurchaseVersion") if relation(item, k)]
    if len(values) != 1:
        raise ReleaseError("Review submission contains an unsupported item; inspect it in App Store Connect")
    return values[0]


def review_plan(snap):
    targets = targets_for(snap)
    blockers = []
    if state(snap["appVersion"]) not in EDITABLE:
        blockers.append(f"App version is not editable: {state(snap['appVersion'])}")
    for p in snap["products"]:
        if p["versionId"] and p["versionState"] not in EDITABLE:
            blockers.append(f"{p['productId']} is not editable: {p['versionState']}")
    unresolved, drafts = [], []
    for sub in snap["submissions"]:
        if state(sub) in ACTIVE_REVIEW:
            blockers.append(f"An iOS review is active ({state(sub)}); it will not be canceled")
            continue
        live = [i for i in sub["items"] if state(i) != "REMOVED"]
        try:
            seen = [item_target(i) for i in live]
        except ReleaseError as exc:
            blockers.append(str(exc))
            continue
        if any(t not in targets for t in seen) or len({json.dumps(t, sort_keys=True) for t in seen}) != len(seen):
            blockers.append("An active submission has unrelated or duplicate items")
        if state(sub) == "UNRESOLVED_ISSUES":
            if any(state(i) not in {"REJECTED", "READY_FOR_REVIEW"} for i in live):
                blockers.append("An unresolved submission contains an item that cannot safely be moved")
            unresolved.append(sub)
        elif state(sub) == "READY_FOR_REVIEW":
            drafts.append(sub)
        else:
            blockers.append(f"Unsupported submission state: {state(sub)}")
    if len(drafts) > 1:
        blockers.append("Multiple iOS drafts exist; select the intended draft in App Store Connect")
    return {"targets": targets, "unresolved": unresolved, "draft": drafts[0] if len(drafts) == 1 else None,
            "blockers": blockers}


def public_status(snap):
    plan = review_plan(snap)
    ready, missing = readiness_report()
    attached = snap["attachedBuild"] or {}
    return {"checkedAt": datetime.now(timezone.utc).isoformat(), "appId": snap["appId"],
            "bundleId": snap["bundleId"], "version": snap["version"],
            "state": state(snap["appVersion"]), "releaseType": _attr(snap["appVersion"], "releaseType"),
            "attachedBuild": {"id": attached.get("id"), "number": _attr(attached, "version"),
                              "processingState": _attr(attached, "processingState")},
            "nextBuildNumber": snap["nextBuildNumber"], "products": snap["products"],
            "submissions": [{"id": s["id"], "state": state(s),
                             "items": [{"id": i["id"], "state": state(i),
                                        "appVersion": relation(i, "appStoreVersion"),
                                        "purchaseVersion": relation(i, "inAppPurchaseVersion")} for i in s["items"]]}
                            for s in snap["submissions"]],
            "submissionBlockers": plan["blockers"],
            "resolutionNotesRequired": bool(plan["unresolved"] or state(snap["appVersion"]) in {"REJECTED", "METADATA_REJECTED"}),
            "readiness": {"ready": ready, "missing": missing},
            "reviewUrl": f"https://appstoreconnect.apple.com/apps/{snap['appId']}/distribution/reviewsubmissions"}


def signing_inputs(config):
    """Verify certificate, private key, bundle, team, expiry, and push capability."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import pkcs12
    apple = config["apple"]
    for key in ("p12Path", "profilePath", "keyPath"):
        if not Path(apple.get(key, "")).expanduser().is_file():
            raise ReleaseError(f"Missing Apple {key}")
    password = apple.get("p12Password", "").encode()
    try:
        key, cert, _ = pkcs12.load_key_and_certificates(Path(apple["p12Path"]).read_bytes(), password)
        if key is None or cert is None:
            raise ValueError("No signing identity")
    except (ValueError, TypeError) as exc:
        raise ReleaseError("Cannot open Apple P12 with configured password") from exc
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ReleaseError("Apple distribution certificate is not currently valid")
    proc = subprocess.run(["security", "cms", "-D", "-i", apple["profilePath"]], capture_output=True)
    if proc.returncode:
        raise ReleaseError("Cannot decode Apple provisioning profile")
    profile = plistlib.loads(proc.stdout)
    ent = profile.get("Entitlements", {})
    if ent.get("application-identifier") != f"{apple['teamId']}.{config['bundleId']}" or apple["teamId"] not in profile.get("TeamIdentifier", []):
        raise ReleaseError("Provisioning profile does not match the configured team/bundle")
    if profile["ExpirationDate"].replace(tzinfo=timezone.utc) <= now:
        raise ReleaseError("Provisioning profile has expired")
    if ent.get("get-task-allow") or profile.get("ProvisionedDevices") or profile.get("ProvisionsAllDevices"):
        raise ReleaseError("An App Store distribution profile is required")
    fingerprint = cert.fingerprint(hashes.SHA256())
    if fingerprint not in [x509.load_der_x509_certificate(c).fingerprint(hashes.SHA256()) for c in profile.get("DeveloperCertificates", [])]:
        raise ReleaseError("Provisioning profile does not contain the configured certificate")
    if ent.get("aps-environment") != "production":
        raise ReleaseError("A push-enabled release requires a production APNs profile")
    # Existing macOS-compatible export is used only if it has the SAME identity.
    p12_path = Path(apple["p12Path"])
    for candidate in (ROOT / ".private/distribution-legacy.p12",
                      ROOT / "handoff/credentials/apple/distribution-legacy.p12"):
        if candidate.is_file():
            try:
                legacy_key, legacy_cert, _ = pkcs12.load_key_and_certificates(candidate.read_bytes(), password)
                if legacy_key and legacy_cert and legacy_cert.fingerprint(hashes.SHA256()) == fingerprint:
                    p12_path = candidate
                    break
            except ValueError:
                pass
    return {"p12Path": str(p12_path), "profilePath": apple["profilePath"],
            "certificateExpires": cert.not_valid_after_utc.isoformat(),
            "profileExpires": profile["ExpirationDate"].isoformat(), "push": True}


def checked_number(value, snap, *, new=False):
    value = str(snap["nextBuildNumber"]) if value == "auto" else str(value)
    if not value.isdigit() or not 1 <= int(value) <= 2100000000:
        raise ReleaseError("Build number must be a positive integer")
    if new and int(value) <= snap["latestBuildNumber"]:
        raise ReleaseError("Build number already used; choose auto or a higher number")
    return value


def upload(ipa, config):
    """Use only the configured API key; no testers or email side effects."""
    apple = config["apple"]
    with tempfile.TemporaryDirectory(prefix="app-upload-") as directory:
        keys = Path(directory) / "private_keys"
        keys.mkdir(mode=0o700)
        target = keys / f"AuthKey_{apple['keyId']}.p8"
        target.write_bytes(Path(apple["keyPath"]).read_bytes())
        target.chmod(0o600)
        proc = subprocess.run(["xcrun", "altool", "--upload-app", "--type", "ios", "--file", str(ipa),
                               "--apiKey", apple["keyId"], "--apiIssuer", apple["issuerId"]],
                              cwd=directory, capture_output=True, text=True, timeout=3600)
    if proc.returncode:
        # Do not swallow duplicate uploads: identity alone does not prove equal bytes.
        raise ReleaseError("Apple upload failed; inspect ASC build uploads before retrying. Use --skip-upload only for the same accepted IPA")


def compatible_p12(source, password, target):
    """Export the same identity with the encryption supported by macOS security."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import PrivateFormat, pkcs12
    key, cert, chain = pkcs12.load_key_and_certificates(Path(source).read_bytes(), password.encode())
    encryption = (PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
                  .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
                  .hmac_hash(hashes.SHA1()).build(password.encode()))
    target.write_bytes(pkcs12.serialize_key_and_certificates(b"App Distribution", key, cert, chain, encryption))
    target.chmod(0o600)


def resolution_notes(path):
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    if len(text) < 20:
        raise ReleaseError("Resolution notes must explain the actual rejection and changes")
    return text


def scoped_review_readiness(client, path, snap, number):
    """Validate evidence for this login rejection without declaring global readiness."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Cannot read iOS review evidence JSON") from exc
    if isinstance(manifest, dict) and manifest.get("scope") == "ios-feature-release":
        return feature_review_readiness(manifest, path, snap, number)
    if not isinstance(manifest, dict) or manifest.get("scope") != "ios-rejection-resubmission":
        raise ReleaseError("Review evidence must declare the ios-rejection-resubmission scope")
    for key, expected in (("appId", snap["appId"]), ("version", snap["version"]), ("buildNumber", number)):
        if manifest.get(key) != expected:
            raise ReleaseError(f"Review evidence {key} does not match the selected release")
    source_id = manifest.get("sourceSubmissionId")
    # This history came from /apps/{appId}/reviewSubmissions, proving app ownership.
    sources = [sub for sub in snap["submissionHistory"]
               if sub["id"] == source_id and _attr(sub, "platform") in (None, "IOS")]
    unresolved = [sub["id"] for sub in snap["submissions"] if state(sub) == "UNRESOLVED_ISSUES"]
    if len(sources) != 1 or any(sid != source_id for sid in unresolved):
        raise ReleaseError("Review evidence does not match the current unresolved submission or app-owned history")
    source = sources[0]
    if state(source) == "UNRESOLVED_ISSUES":
        if unresolved != [source_id]:
            raise ReleaseError("Review evidence does not match the current unresolved submission")
    elif state(source) == "COMPLETE" and not unresolved:
        # A prior attempt may have removed the rejected item before its next API
        # request failed. Historical removed items still identify the app version.
        source_items = client.all(f"/v1/reviewSubmissions/{source_id}/items",
                                  {"limit": "200", "include": RELATIONS})
        expected_version = {"type": "appStoreVersions", "id": snap["appVersion"]["id"]}
        if not any(relation(item, "appStoreVersion") == expected_version for item in source_items):
            raise ReleaseError("Completed source review does not belong to the selected app version")
        # review_plan below validates any existing draft as a unique subset of
        # these targets; exact membership is still required before submission.
    else:
        raise ReleaseError("Source review is neither the current unresolved submission nor a completed review")
    if manifest.get("guideline") != "2.1(a)" or not isinstance(manifest.get("currentIssue"), str) or not manifest["currentIssue"].strip():
        raise ReleaseError("This evidence scope requires the current Guideline 2.1(a) login issue")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or any(name not in evidence for name in LOGIN_REVIEW_EVIDENCE):
        raise ReleaseError("Review evidence requires rejectionSource, signedBuildIdentity, reviewerApiLogin, and releaseSimulatorLogin")
    for name, check in evidence.items():
        if (not isinstance(check, dict) or check.get("status") != "passed"
                or not isinstance(check.get("detail"), str) or not check["detail"].strip()):
            raise ReleaseError(f"Review evidence {name} must have status passed and a nonempty detail")
    limitations = manifest.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(not isinstance(item, str) or not item.strip() for item in limitations):
        raise ReleaseError("Scoped review evidence must retain nonempty limitations")
    return {"scope": manifest["scope"], "sourceSubmissionId": manifest["sourceSubmissionId"],
            "guideline": manifest["guideline"], "sourceState": state(source),
            "evidenceFile": str(Path(path).resolve()),
            "limitations": limitations}


def feature_review_readiness(manifest, path, snap, number):
    """Bind a requested feature release to actual tested bytes, retaining limitations."""
    for key, expected in (("appId", snap["appId"]), ("version", snap["version"]), ("buildNumber", number)):
        if manifest.get(key) != expected:
            raise ReleaseError(f"Feature evidence {key} does not match the selected release")
    if (manifest.get("scope") != "ios-feature-release"
            or not isinstance(manifest.get("requestedChange"), str) or not manifest["requestedChange"].strip()):
        raise ReleaseError("Feature evidence must describe the requested change")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or any(name not in evidence for name in FEATURE_REVIEW_EVIDENCE):
        raise ReleaseError("Feature evidence is missing required validation")
    for name, check in evidence.items():
        if (not isinstance(check, dict) or check.get("status") != "passed"
                or not isinstance(check.get("detail"), str) or not check["detail"].strip()):
            raise ReleaseError(f"Feature evidence {name} must be passed with details")
    limitations = manifest.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(not isinstance(v, str) or not v.strip() for v in limitations):
        raise ReleaseError("Feature evidence must retain explicit validation limitations")
    artifact = manifest.get("artifact", {})
    ipa = (ROOT / str(artifact.get("path", ""))).resolve()
    if not ipa.is_relative_to((ROOT / "release-artifacts").resolve()) or not ipa.is_file():
        raise ReleaseError("Feature evidence must identify an existing release-artifacts IPA")
    if hashlib.sha256(ipa.read_bytes()).hexdigest() != artifact.get("sha256"):
        raise ReleaseError("Feature evidence IPA hash differs from the tested artifact")
    if ipa_identity(ipa) != (snap["bundleId"], snap["version"], number):
        raise ReleaseError("Feature evidence IPA identity differs from the release")
    return {"scope": manifest["scope"], "evidenceFile": str(Path(path).resolve()),
            "artifactSha256": artifact["sha256"], "limitations": limitations}


def prepare_version(client, config, args):
    """Explicitly replace the named review only after the replacement build is valid.

    This command is opt-in. Ordinary plan/release/submit never cancel reviews.
    Live reviewer access, storefront metadata and release mode remain untouched.
    """
    bundle = require_project_identity(config, "ios")
    app_id = str(config["apple"].get("appId") or resolve_app_id(client, bundle))
    if _attr(get(client, f"/v1/apps/{app_id}"), "bundleId") != bundle:
        raise ReleaseError("Apple app does not match this project's bundle ID")
    version = json.loads((ROOT / "app.json").read_text())["expo"]["version"]
    number = str(args.build_number)
    manifest = json.loads(Path(args.review_evidence).read_text())
    feature_review_readiness(manifest, args.review_evidence,
                             {"appId": app_id, "bundleId": bundle, "version": version}, number)
    versions = client.all(f"/v1/apps/{app_id}/appStoreVersions", {"limit": "200"})
    matches = [v for v in versions if v["id"] == args.app_version_id and _attr(v, "platform") == "IOS"
               and _attr(v, "versionString") in {args.from_version, version}]
    if len(matches) != 1:
        raise ReleaseError("The source app version is not owned by this app or has changed")
    selected = matches[0]
    release_type = _attr(selected, "releaseType")
    builds = client.all("/v1/builds", {"filter[app]": app_id, "filter[version]": number, "limit": "200"})
    candidates = []
    for build in builds:
        pre = get(client, f"/v1/builds/{build['id']}/preReleaseVersion")
        if _attr(build, "version") == number and (_attr(pre, "version"), _attr(pre, "platform")) == (version, "IOS"):
            candidates.append(build)
    if len(candidates) != 1:
        raise ReleaseError("Expected exactly one replacement build")
    build = candidates[0]
    if (_attr(build, "processingState") != "VALID" or _attr(build, "expired")
            or _attr(build, "usesNonExemptEncryption") is not False):
        raise ReleaseError("Replacement build must be VALID, unexpired, and export-compliant")
    history = client.all(f"/v1/apps/{app_id}/reviewSubmissions", {"limit": "200"})
    sources = [s for s in history if s["id"] == args.source_submission_id and _attr(s, "platform") == "IOS"]
    if len(sources) != 1 or any(s["id"] != args.source_submission_id and state(s) in ACTIVE_REVIEW for s in history):
        raise ReleaseError("Source review ownership changed or another review is active")
    source = sources[0]
    items = client.all(f"/v1/reviewSubmissions/{source['id']}/items", {"limit": "200", "include": RELATIONS})
    app_target = {"type": "appStoreVersions", "id": selected["id"]}
    if not any(relation(i, "appStoreVersion") == app_target for i in items):
        raise ReleaseError("Source review does not contain the selected app version")
    if any(relation(i, "appStoreVersion") not in (None, app_target) for i in items):
        raise ReleaseError("Source review contains another app version")
    # A named review can group additional purchases. Prove each IAP version
    # belongs to one of this app's intended consumables before withdrawing it.
    allowed = [("appStoreVersion", app_target)]
    available = client.all(f"/v1/apps/{app_id}/inAppPurchasesV2", {"limit": "200"})
    for product_id in PRODUCTS:
        found = [p for p in available if _attr(p, "productId") == product_id]
        if len(found) != 1 or _attr(found[0], "inAppPurchaseType") != "CONSUMABLE":
            raise ReleaseError(f"Expected one app-owned consumable product: {product_id}")
        purchase_versions = client.all(f"/v2/inAppPurchases/{found[0]['id']}/versions", {"limit": "200"})
        allowed.extend(("inAppPurchaseVersion", {"type": "inAppPurchaseVersions", "id": v["id"]})
                       for v in purchase_versions)
    for item in items:
        if item_target(item) not in allowed:
            raise ReleaseError("Source review contains an unrelated item or purchase version")
    if state(source) in {"WAITING_FOR_REVIEW", "IN_REVIEW"}:
        client.request("PATCH", f"/v1/reviewSubmissions/{source['id']}", json_body={"data": {
            "type": "reviewSubmissions", "id": source["id"], "attributes": {"canceled": True}}})
    elif state(source) not in {"CANCELING", "CANCELED", "COMPLETE"}:
        raise ReleaseError("Source review cannot be replaced in its current state")
    deadline = time.monotonic() + 180
    while True:
        current = get(client, f"/v1/appStoreVersions/{selected['id']}")
        old = get(client, f"/v1/reviewSubmissions/{source['id']}")
        if state(old) in {"CANCELED", "COMPLETE"} and state(current) in EDITABLE:
            break
        if time.monotonic() >= deadline:
            raise ReleaseError("Cancellation is pending; rerun prepare-version after it completes")
        time.sleep(3)
    vid = selected["id"]
    if _attr(current, "versionString") != version:
        client.request("PATCH", f"/v1/appStoreVersions/{vid}/relationships/build", json_body={"data": None})
        client.request("PATCH", f"/v1/appStoreVersions/{vid}", json_body={"data": {
            "type": "appStoreVersions", "id": vid, "attributes": {"versionString": version}}})
    client.request("PATCH", f"/v1/appStoreVersions/{vid}/relationships/build", json_body={"data": {
        "type": "builds", "id": build["id"]}})
    confirmed = get(client, f"/v1/appStoreVersions/{vid}")
    if (_attr(confirmed, "versionString") != version or _attr(confirmed, "releaseType") != release_type
            or get(client, f"/v1/appStoreVersions/{vid}/build")["id"] != build["id"]):
        raise ReleaseError("Version replacement readback did not match")
    return {"version": version, "buildNumber": number, "appVersionId": vid,
            "sourceSubmissionId": source["id"], "sourceState": state(old),
            "state": state(confirmed), "releaseType": release_type}


def submission_preflight(client, snap, notes, *, number=None, review_evidence_path=None):
    if review_evidence_path is None:
        require_ready()
        readiness = {"scope": "global-release-readiness"}
    else:
        readiness = scoped_review_readiness(client, review_evidence_path, snap, number)
    plan = review_plan(snap)
    if plan["blockers"]:
        raise ReleaseError("; ".join(plan["blockers"]))
    if (plan["unresolved"] or state(snap["appVersion"]) in {"REJECTED", "METADATA_REJECTED"}) and not notes:
        raise ReleaseError("Read the latest rejection and supply --resolution-notes-file before resubmission")
    detail = get(client, f"/v1/appStoreVersions/{snap['appVersion']['id']}/appStoreReviewDetail")
    attrs = detail.get("attributes", {})
    needed = ["contactFirstName", "contactLastName", "contactEmail", "contactPhone"]
    if attrs.get("demoAccountRequired"):
        needed += ["demoAccountName", "demoAccountPassword"]
    if any(not attrs.get(k) for k in needed):
        raise ReleaseError("Live App Review contact/demo account fields are incomplete")
    existing = attrs.get("notes") or ""
    combined = existing if not notes or notes in existing else existing + "\n\n" + notes
    if len(combined) > 4000:
        raise ReleaseError("Combined review notes exceed 4000 characters; edit live notes without dropping review access")
    return plan, detail, combined, readiness


def wait_removed(client, submission_id, item_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Apple supports listing a submission's items, but not singleton GET
        # /reviewSubmissionItems/{id} (that endpoint only supports mutations).
        items = client.all(f"/v1/reviewSubmissions/{submission_id}/items", {"limit": "200"})
        matches = [item for item in items if item.get("id") == item_id]
        if len(matches) == 1 and state(matches[0]) == "REMOVED":
            return
        time.sleep(3)
    raise ReleaseError("Apple is still removing the old review item; rerun submit after removal completes")


def wait_review_editable(client, submission_ids, version_id, timeout=120):
    """Item removal can precede completion of its old review submission."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        old_states = [state(get(client, f"/v1/reviewSubmissions/{sid}")) for sid in submission_ids]
        version_state = state(get(client, f"/v1/appStoreVersions/{version_id}"))
        if all(value == "COMPLETE" for value in old_states) and version_state in EDITABLE:
            return
        time.sleep(3)
    raise ReleaseError("Apple has not completed the old review and unlocked the app version; run status before retrying")


def submit(client, config, number, notes=None, *, review_evidence_path=None):
    snap = snapshot(client, config)  # Refresh immediately before any remote mutation.
    number = checked_number(number, snap)
    plan, detail, combined, readiness = submission_preflight(
        client, snap, notes, number=number, review_evidence_path=review_evidence_path)
    matches = []
    for build in snap["builds"]:
        if _attr(build, "version") != number:
            continue
        prerelease = get(client, f"/v1/builds/{build['id']}/preReleaseVersion")
        owner = get(client, f"/v1/builds/{build['id']}/app")
        if _attr(prerelease, "version") == snap["version"] and _attr(prerelease, "platform") == "IOS" and owner["id"] == snap["appId"]:
            matches.append(build)
    if len(matches) != 1 or _attr(matches[0], "processingState") != "VALID" or _attr(matches[0], "expired"):
        raise ReleaseError("Expected exactly one VALID, unexpired build of this app/version")
    build = matches[0]
    if _attr(build, "usesNonExemptEncryption") is not False:
        raise ReleaseError("Export compliance must be completed for this build")
    vid = snap["appVersion"]["id"]
    # Preserve live private login fields and the existing releaseType.
    if combined != (_attr(detail, "notes") or ""):
        client.request("PATCH", f"/v1/appStoreReviewDetails/{detail['id']}", json_body={"data": {
            "type": "appStoreReviewDetails", "id": detail["id"], "attributes": {"notes": combined}}})
    for old in plan["unresolved"]:
        for item in old["items"]:
            if state(item) == "REMOVED":
                continue
            client.request("PATCH", f"/v1/reviewSubmissionItems/{item['id']}", json_body={"data": {
                "type": "reviewSubmissionItems", "id": item["id"], "attributes": {"removed": True}}})
            wait_removed(client, old["id"], item["id"])
    if plan["unresolved"]:
        wait_review_editable(client, [old["id"] for old in plan["unresolved"]], vid)
    client.request("PATCH", f"/v1/appStoreVersions/{vid}/relationships/build", json_body={
        "data": {"type": "builds", "id": build["id"]}})
    draft = plan["draft"]
    if draft is None:
        draft = _single_data(client.request("POST", "/v1/reviewSubmissions", json_body={"data": {
            "type": "reviewSubmissions", "attributes": {"platform": "IOS"},
            "relationships": {"app": {"data": {"type": "apps", "id": snap["appId"]}}}}}), "new review")
    sid = draft["id"]
    current = client.all(f"/v1/reviewSubmissions/{sid}/items", {"limit": "200", "include": RELATIONS})
    present = [item_target(i) for i in current if state(i) != "REMOVED"]
    for kind, target in plan["targets"]:
        if (kind, target) not in present:
            client.request("POST", "/v1/reviewSubmissionItems", json_body={"data": {
                "type": "reviewSubmissionItems", "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sid}}, kind: {"data": target}}}})
    # Verify exact membership before the consequential final submit request.
    items = client.all(f"/v1/reviewSubmissions/{sid}/items", {"limit": "200", "include": RELATIONS})
    actual = [item_target(i) for i in items if state(i) != "REMOVED"]
    if len(actual) != len(plan["targets"]) or any(t not in actual for t in plan["targets"]):
        raise ReleaseError("Review item membership did not match the app plus required consumables")
    if get(client, f"/v1/appStoreVersions/{vid}/build")["id"] != build["id"]:
        raise ReleaseError("Attached build changed before submission")
    client.request("PATCH", f"/v1/reviewSubmissions/{sid}", json_body={"data": {
        "type": "reviewSubmissions", "id": sid, "attributes": {"submitted": True}}})
    confirmed = get(client, f"/v1/reviewSubmissions/{sid}")
    version = get(client, f"/v1/appStoreVersions/{vid}")
    if state(confirmed) not in {"WAITING_FOR_REVIEW", "IN_REVIEW"} or state(version) not in {"WAITING_FOR_REVIEW", "IN_REVIEW"}:
        raise ReleaseError("Submission requested but not yet confirmed; run status before retrying")
    final_items = client.all(f"/v1/reviewSubmissions/{sid}/items", {"limit": "200", "include": RELATIONS})
    final_live = [i for i in final_items if state(i) != "REMOVED"]
    final_targets = [item_target(i) for i in final_live]
    # Item state remains READY_FOR_REVIEW while the submission is in the queue;
    # WAITING_FOR_REVIEW/IN_REVIEW belong to the parent submission/app version.
    if (len(final_targets) != len(plan["targets"])
            or any(target not in final_targets for target in plan["targets"])
            or any(state(i) not in {"READY_FOR_REVIEW", "ACCEPTED", "APPROVED"} for i in final_live)):
        raise ReleaseError("Submission requested; item review states are not confirmed yet. Run status before retrying")
    return {"submissionId": sid, "buildId": build["id"], "buildNumber": number,
            "state": state(confirmed), "appState": state(version), "itemCount": len(actual),
            "releaseType": _attr(version, "releaseType"), "readiness": readiness}


@contextlib.contextmanager
def release_lock():
    directory = ROOT / ".private"
    directory.mkdir(exist_ok=True)
    with (directory / "ios-release.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("Another local iOS release is running") from exc
        yield


def release(client, config, args):
    # Upload a new marketing-version build before interrupting an existing
    # review. Linking/submission still requires an editable ASC version.
    snap = snapshot(client, config, allow_missing_version=not args.submit)
    notes = resolution_notes(args.resolution_notes_file)
    if args.submit:
        submission_preflight(client, snap, notes)
    if args.skip_upload and not args.ipa:
        raise ReleaseError("--skip-upload requires the exact previously accepted --ipa")
    if args.ipa:
        ipa = Path(args.ipa).resolve()
        bundle, version, number = ipa_identity(ipa)
        if (bundle, version) != (snap["bundleId"], snap["version"]):
            raise ReleaseError("IPA bundle/version does not match the project and App Store")
        if args.build_number != "auto" and args.build_number != number:
            raise ReleaseError("Requested build number does not match the IPA")
        checked_number(number, snap, new=not args.skip_upload)
    else:
        number = checked_number(args.build_number, snap, new=True)
        signing = signing_inputs(config)
        env = os.environ.copy()
        env.update({"APP_BUILD_NUMBER": number, "APP_PUSH": "1", "IOS_BUILD_MODE": "device",
                    "APPLE_TEAM_ID": config["apple"]["teamId"], "IOS_P12_PATH": signing["p12Path"],
                    "IOS_P12_PASSWORD": config["apple"]["p12Password"], "IOS_PROFILE_PATH": signing["profilePath"]})
        destination = ROOT / "release-artifacts" / f"app-ios-build{number}.ipa"
        if destination.exists():
            raise ReleaseError("Numbered IPA already exists; use --ipa to resume it")
        with tempfile.TemporaryDirectory(prefix="app-signing-") as directory:
            compatible = Path(directory) / "distribution.p12"
            compatible_p12(signing["p12Path"], config["apple"]["p12Password"], compatible)
            env["IOS_P12_PATH"] = str(compatible)
            subprocess.run(["bash", str(ROOT / "scripts/build_ios.sh")], cwd=ROOT, env=env, check=True)
        source = ROOT / "release-artifacts/app-ios-release.ipa"
        if ipa_identity(source) != (snap["bundleId"], snap["version"], number):
            raise ReleaseError("Built IPA identity differs from the requested release")
        ipa = destination
        shutil.copy2(source, ipa)
    digest = hashlib.sha256(ipa.read_bytes()).hexdigest()
    report_path = ROOT / "release-artifacts" / f"ios-build{number}-report.json"
    if args.skip_upload and report_path.is_file():
        prior = json.loads(report_path.read_text())
        if prior.get("sha256") != digest:
            raise ReleaseError("IPA differs from the previously recorded upload; refusing to resume")
    report = {"appId": snap["appId"], "version": snap["version"], "buildNumber": number,
              "ipa": str(ipa), "sha256": digest, "submitted": False}
    report_path.parent.mkdir(exist_ok=True)
    # Record bytes before upload, so a timeout/processing delay can be resumed.
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if not args.skip_upload:
        print(f"Uploading iOS {snap['version']} ({number}) ...", flush=True)
        upload(ipa, config)
    build = wait_for_build(client, snap["appId"], snap["version"], number)
    report.update(buildId=build["id"], processingState=_attr(build, "processingState"))
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if args.submit:
        report["review"] = submit(client, config, number, notes)
        report["submitted"] = True
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--output", help="Write the credential-free result JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    plan = commands.add_parser("plan")
    plan.add_argument("--build-number", default="auto")
    run = commands.add_parser("release")
    run.add_argument("--build-number", default="auto")
    run.add_argument("--ipa")
    run.add_argument("--skip-upload", action="store_true")
    run.add_argument("--submit", action="store_true")
    run.add_argument("--resolution-notes-file")
    review = commands.add_parser("submit")
    review.add_argument("--build-number", required=True)
    review.add_argument("--resolution-notes-file")
    review.add_argument("--review-evidence", help="Build-bound evidence JSON for a scoped rejection fix or requested feature release")
    replacement = commands.add_parser("prepare-version", help="Explicitly replace a named review with a tested, processed new version")
    replacement.add_argument("--app-version-id", required=True)
    replacement.add_argument("--source-submission-id", required=True)
    replacement.add_argument("--from-version", required=True)
    replacement.add_argument("--build-number", required=True)
    replacement.add_argument("--review-evidence", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        client = AppleClient(config)
        if args.command in {"status", "plan"}:
            snap = snapshot(client, config)
            result = public_status(snap)
            if args.command == "plan":
                result["plannedBuildNumber"] = checked_number(args.build_number, snap, new=True)
                result["signing"] = signing_inputs(config)
                result["steps"] = ["build signed push-enabled IPA", "verify IPA identity", "upload",
                                   "wait for VALID", "optional review: app + both unapproved consumables"]
        else:
            with release_lock():
                if args.command == "prepare-version":
                    result = prepare_version(client, config, args)
                elif args.command == "release":
                    result = release(client, config, args)
                else:
                    result = submit(client, config, args.build_number, resolution_notes(args.resolution_notes_file),
                                    review_evidence_path=args.review_evidence)
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output)
        print(output, end="")
        return 0
    except (ReleaseError, OSError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print(f"iOS release error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
