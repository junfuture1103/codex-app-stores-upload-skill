"""Guarded App Store Connect and Google Play release operations.

Credentials are read from a local JSON file. This module never prints key material.
The default paths are relative to the repository, not to the current shell directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
APPLE_API = "https://api.appstoreconnect.apple.com"
GOOGLE_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
REQUIRED_EVIDENCE = (
    "iosDeviceTest",
    "androidDeviceTest",
    "storeMetadata",
    "privacyDisclosures",
    "contentRights",
    "reviewAccess",
)
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ReleaseError(Exception):
    """Actionable error that is safe to show in a terminal."""


class ApiError(ReleaseError):
    """HTTP failure with safe status, machine code and known remediation."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt(header: dict[str, Any], payload: dict[str, Any], key_pem: bytes, algorithm: str) -> str:
    """Sign a compact JWT using cryptography, without writing a key to disk."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, utils

    message = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    private_key = serialization.load_pem_private_key(key_pem, password=None)
    if algorithm == "ES256":
        der = private_key.sign(message.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    elif algorithm == "RS256":
        signature = private_key.sign(message.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    else:
        raise ReleaseError("Unsupported signing algorithm")
    return message + "." + _b64(signature)


def _json_file(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Cannot read JSON file: {path}") from exc
    if not isinstance(result, dict):
        raise ReleaseError(f"Expected a JSON object: {path}")
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load local release configuration; all filesystem secrets stay local."""
    config_path = Path(path or os.environ.get("RELEASE_CONFIG") or ROOT / ".private" / "release-config.json")
    config = _json_file(config_path)
    for section in ("apple", "google", "android"):
        if not isinstance(config.get(section), dict):
            raise ReleaseError(f"Missing {section} object in release config")
    package = config.get("bundleId")
    if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
        raise ReleaseError("Invalid bundleId in release config")
    return config


def _required_string(obj: dict[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"Missing {label}.{key} in release config")
    return value.strip()


def _key_path(obj: dict[str, Any], key: str, label: str) -> Path:
    path = Path(_required_string(obj, key, label)).expanduser()
    if not path.is_file():
        raise ReleaseError(f"{label}.{key} file does not exist")
    return path


def _api_code(response: Any) -> str | None:
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        for key in ("status", "code"):
            value = error.get(key)
            if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
                return value
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        value = errors[0].get("code")
        if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
            return value
    return None


def _request_json(session: Any, service: str, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Send a request; never echo response bodies, URLs with query secrets, or headers."""
    try:
        response = session.request(method, url, timeout=60, **kwargs)
    except Exception as exc:
        raise ApiError(f"{service} transport failed ({type(exc).__name__})") from exc
    if not 200 <= response.status_code < 300:
        code = _api_code(response)
        suffix = f" ({code})" if code else ""
        if service == "Google Play" and response.status_code == 403 and code == "PERMISSION_DENIED":
            error = response.json().get("error")
            if isinstance(error, dict) and error.get("message") == "Please migrate to the new publishing API.":
                suffix += (
                    ": publishing API migration required, not a permission propagation issue. "
                    "Use /oneTimeProducts instead of /inappproducts; "
                    "run store_release.py android-products. Administrator access is not a migration fix."
                )
        raise ApiError(f"{service} HTTP {response.status_code}{suffix}")
    if not getattr(response, "content", b""):
        return {}
    try:
        body = response.json()
    except Exception as exc:
        raise ApiError(f"{service} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise ApiError(f"{service} returned an unexpected response")
    return body


class AppleClient:
    def __init__(self, config: dict[str, Any], session: Any = None) -> None:
        import requests

        self.config = config.get("apple", config)
        self.session = session or requests.Session()
        self._token_value = ""
        self._token_until = 0

    def _token(self) -> str:
        now = int(time.time())
        if now < self._token_until - 60:
            return self._token_value
        apple = self.config
        key_id = _required_string(apple, "keyId", "apple")
        issuer = _required_string(apple, "issuerId", "apple")
        key_pem = _key_path(apple, "keyPath", "apple").read_bytes()
        self._token_value = _jwt(
            {"alg": "ES256", "kid": key_id, "typ": "JWT"},
            {"iss": issuer, "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
            key_pem,
            "ES256",
        )
        self._token_until = now + 600
        return self._token_value

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith(("/v1/", "/v2/")) or ".." in path.split("?")[0].split("/"):
            raise ReleaseError("Invalid Apple API path")
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
        }
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
            kwargs["headers"]["Content-Type"] = "application/json"
        return _request_json(self.session, "App Store Connect", method, APPLE_API + path, **kwargs)

    def all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Follow Apple's JSON:API pagination, restricting next URLs to Apple."""
        items: list[dict[str, Any]] = []
        next_path: str | None = path
        next_params = params
        while next_path:
            page = self.request("GET", next_path, params=next_params)
            data = page.get("data", [])
            if not isinstance(data, list):
                raise ApiError("App Store Connect returned an unexpected list")
            items.extend(data)
            next_url = (page.get("links") or {}).get("next")
            if next_url:
                parsed = urlparse(next_url)
                if parsed.scheme != "https" or parsed.netloc != "api.appstoreconnect.apple.com" or not parsed.path.startswith(("/v1/", "/v2/")):
                    raise ApiError("App Store Connect returned an invalid next page")
                next_path = parsed.path + ("?" + parsed.query if parsed.query else "")
                next_params = None
            else:
                next_path = None
        return items


class GoogleClient:
    def __init__(self, config: dict[str, Any], session: Any = None) -> None:
        import requests

        self.config = config.get("google", config)
        self.session = session or requests.Session()
        self._token_value = ""
        self._token_until = 0
        self.service_email = ""

    def _token(self) -> str:
        now = int(time.time())
        if now < self._token_until - 60:
            return self._token_value
        account = _json_file(_key_path(self.config, "serviceAccountPath", "google"))
        email = _required_string(account, "client_email", "serviceAccount")
        key = _required_string(account, "private_key", "serviceAccount").encode()
        self.service_email = email
        assertion = _jwt(
            {"alg": "RS256", "typ": "JWT"},
            {"iss": email, "scope": GOOGLE_SCOPE, "aud": GOOGLE_TOKEN_URL, "iat": now, "exp": now + 3600},
            key,
            "RS256",
        )
        body = _request_json(
            self.session, "Google OAuth", "POST", GOOGLE_TOKEN_URL,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        value = body.get("access_token")
        if not isinstance(value, str) or not value:
            raise ApiError("Google OAuth response did not include an access token")
        self._token_value = value
        self._token_until = now + min(int(body.get("expires_in", 3600)), 3600)
        return value

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, data: Any = None, upload: bool = False) -> dict[str, Any]:
        if not path.startswith("/applications/") and not path.startswith("/developers/"):
            raise ReleaseError("Invalid Google Play API path")
        base = GOOGLE_UPLOAD_API if upload else GOOGLE_API
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
        }
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
            kwargs["headers"]["Content-Type"] = "application/octet-stream"
        return _request_json(self.session, "Google Play", method, base + path, **kwargs)


def project_package(platform: str) -> str:
    app = _json_file(ROOT / "app.json").get("expo")
    if not isinstance(app, dict):
        raise ReleaseError("app.json is missing the expo object")
    section = app.get(platform)
    if not isinstance(section, dict):
        raise ReleaseError(f"app.json is missing expo.{platform}")
    field = "bundleIdentifier" if platform == "ios" else "package"
    value = section.get(field)
    if not isinstance(value, str) or not PACKAGE_RE.fullmatch(value):
        raise ReleaseError(f"app.json has invalid expo.{platform}.{field}")
    return value


def require_project_identity(config: dict[str, Any], platform: str) -> str:
    configured = _required_string(config, "bundleId", "release config")
    actual = project_package(platform)
    if actual != configured:
        raise ReleaseError(f"app.json {platform} package does not match release config bundleId")
    return configured


def readiness_report(path: str | Path = ROOT / "release" / "readiness.json") -> tuple[bool, list[str]]:
    """Require an explicit ready flag and nonempty evidence for each release gate."""
    report = _json_file(Path(path))
    missing: list[str] = []
    if report.get("ready") is not True:
        missing.append("ready is not true")
    evidence = report.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    for key in REQUIRED_EVIDENCE:
        value = evidence.get(key)
        if value is not True and (not isinstance(value, str) or not value.strip()):
            missing.append(key)
    return not missing, missing


def require_ready() -> None:
    ready, missing = readiness_report()
    if not ready:
        raise ReleaseError("Release readiness is incomplete: " + ", ".join(missing))


def _single_data(body: dict[str, Any], label: str) -> dict[str, Any]:
    data = body.get("data")
    if not isinstance(data, dict):
        raise ApiError(f"{label} was not found")
    return data


def _attr(resource: dict[str, Any], name: str) -> Any:
    attrs = resource.get("attributes")
    return attrs.get(name) if isinstance(attrs, dict) else None


def _resource_id(resource: dict[str, Any], label: str) -> str:
    value = resource.get("id")
    if not isinstance(value, str) or not value:
        raise ApiError(f"{label} has no ID")
    return value


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ReleaseError(f"Invalid {label}")
    return value


def _apple_app_id(config: dict[str, Any], supplied: str | None) -> str:
    app_id = supplied or config.get("apple", {}).get("appId")
    if not isinstance(app_id, str) or not app_id.isdigit():
        raise ReleaseError("Apple app ID is required; create the app record in App Store Connect first")
    expected = config.get("apple", {}).get("appId")
    if expected and str(expected) != app_id:
        raise ReleaseError("Apple app ID does not match release config")
    return app_id


def _apple_versions(client: Any, app_id: str) -> list[dict[str, Any]]:
    return client.all(f"/v1/apps/{app_id}/appStoreVersions", params={"limit": "200"})


def _apple_submissions(client: Any, app_id: str) -> list[dict[str, Any]]:
    return client.all(f"/v1/apps/{app_id}/reviewSubmissions", params={"limit": "200"})


def _submission_items(client: Any, submission_id: str) -> list[dict[str, Any]]:
    return client.all(f"/v1/reviewSubmissions/{submission_id}/items", params={"limit": "200", "include": "appStoreVersion"})


def _item_version_id(item: dict[str, Any]) -> str | None:
    relationships = item.get("relationships")
    if isinstance(relationships, dict):
        version = relationships.get("appStoreVersion")
        if isinstance(version, dict):
            data = version.get("data")
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                return data["id"]
    return None


def _apple_preflight(client: Any, config: dict[str, Any], app_id: str, build_id: str, version: str) -> tuple[str, str | None, list[dict[str, Any]]]:
    app = _single_data(client.request("GET", f"/v1/apps/{app_id}"), "Apple app")
    if _resource_id(app, "Apple app") != app_id or _attr(app, "bundleId") != config["bundleId"]:
        raise ReleaseError("Apple app bundle ID does not match the project")
    build = _single_data(client.request("GET", f"/v1/builds/{build_id}"), "Apple build")
    if _resource_id(build, "Apple build") != build_id or _attr(build, "processingState") != "VALID" or _attr(build, "expired") is True:
        raise ReleaseError("Apple build is not VALID or has expired")
    owner = _single_data(client.request("GET", f"/v1/builds/{build_id}/app"), "Apple build app")
    if _resource_id(owner, "Apple build app") != app_id:
        raise ReleaseError("Apple build does not belong to the selected app")
    pre_release = _single_data(client.request("GET", f"/v1/builds/{build_id}/preReleaseVersion"), "Apple build version")
    if _attr(pre_release, "version") != version or _attr(pre_release, "platform") != "IOS":
        raise ReleaseError("Apple build does not match the requested iOS version")
    matches = [
        item for item in _apple_versions(client, app_id)
        if _attr(item, "platform") == "IOS" and _attr(item, "versionString") == version
    ]
    if len(matches) != 1:
        raise ReleaseError("Expected exactly one iOS App Store version matching the requested version")
    chosen = matches[0]
    state = _attr(chosen, "appVersionState")
    if state not in ("PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE", "READY_FOR_DISTRIBUTION"):
        raise ReleaseError(f"App Store version is not submittable ({state})")
    if _attr(chosen, "releaseType") != "MANUAL":
        raise ReleaseError("App Store version must use manual release")
    version_id = _resource_id(chosen, "App Store version")
    attached = client.request("GET", f"/v1/appStoreVersions/{version_id}/build").get("data")
    attached_id = _resource_id(attached, "Attached build") if isinstance(attached, dict) else None
    if attached_id and attached_id != build_id:
        raise ReleaseError("App Store version already has a different build; change it manually")
    return version_id, attached_id, _apple_submissions(client, app_id)


def _apple_review_plan(client: Any, submissions: list[dict[str, Any]], version_id: str) -> tuple[str, str | None]:
    """Choose an existing draft or recognize a submitted version without cancellation."""
    draft: str | None = None
    for submission in submissions:
        platform = _attr(submission, "platform")
        if platform not in (None, "IOS"):
            continue
        state = _attr(submission, "state")
        if state in ("CANCELED", "COMPLETE", "REJECTED", "DEVELOPER_REJECTED"):
            continue
        submission_id = _resource_id(submission, "Review submission")
        items = _submission_items(client, submission_id)
        ids = {_item_version_id(item) for item in items}
        if version_id in ids:
            if state == "READY_FOR_REVIEW":
                return "submit-existing", submission_id
            return "already-submitted", submission_id
        if state == "READY_FOR_REVIEW" and not ids and draft is None:
            draft = submission_id
        elif state == "READY_FOR_REVIEW":
            # A draft containing another version must not be altered.
            continue
    return ("reuse-draft", draft) if draft else ("create-and-submit", None)


def apple_review(
    config: dict[str, Any], app_id: str, build_id: str, version: str,
    *, dry_run: bool = False, client: Any = None,
) -> dict[str, Any]:
    """Submit exactly one iOS version and its VALID build for App Review."""
    if not dry_run:
        require_ready()
    require_project_identity(config, "ios")
    app_id = _apple_app_id(config, app_id)
    build_id = _safe_id(build_id, "Apple build ID")
    version = _safe_id(version, "App Store version")
    client = client or AppleClient(config)
    version_id, attached_id, submissions = _apple_preflight(client, config, app_id, build_id, version)
    action, submission_id = _apple_review_plan(client, submissions, version_id)
    result: dict[str, Any] = {
        "platform": "ios", "action": action, "appId": app_id,
        "version": version, "versionId": version_id, "buildId": build_id,
        "submissionId": submission_id, "dryRun": dry_run,
    }
    if action == "already-submitted" or dry_run:
        return result
    if not attached_id:
        client.request("PATCH", f"/v1/appStoreVersions/{version_id}/relationships/build", json_body={
            "data": {"type": "builds", "id": build_id},
        })
    if submission_id is None:
        created = _single_data(client.request("POST", "/v1/reviewSubmissions", json_body={
            "data": {"type": "reviewSubmissions", "attributes": {"platform": "IOS"},
                     "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}},
        }), "Review submission")
        submission_id = _resource_id(created, "Review submission")
    if action != "submit-existing":
        client.request("POST", "/v1/reviewSubmissionItems", json_body={
            "data": {"type": "reviewSubmissionItems", "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
            }},
        })
    client.request("PATCH", f"/v1/reviewSubmissions/{submission_id}", json_body={
        "data": {"type": "reviewSubmissions", "id": submission_id, "attributes": {"submitted": True}},
    })
    result["submissionId"] = submission_id
    return result


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise ReleaseError("Truncated AAB manifest protobuf")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 128:
            return value, offset
        shift += 7
    raise ReleaseError("Invalid AAB manifest protobuf varint")


def _fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    fields: list[tuple[int, int, bytes | int]] = []
    position = 0
    while position < len(data):
        key, position = _varint(data, position)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ReleaseError("Invalid AAB manifest protobuf field")
        if wire == 0:
            value, position = _varint(data, position)
        elif wire == 1:
            value = data[position:position + 8]
            position += 8
        elif wire == 2:
            length, position = _varint(data, position)
            if length > 10_000_000:
                raise ReleaseError("AAB manifest field is too large")
            value = data[position:position + length]
            position += length
            if len(value) != length:
                raise ReleaseError("Truncated AAB manifest protobuf field")
        elif wire == 5:
            value = data[position:position + 4]
            position += 4
        else:
            raise ReleaseError("Unsupported AAB manifest protobuf wire type")
        if position > len(data):
            raise ReleaseError("Truncated AAB manifest protobuf field")
        fields.append((number, wire, value))
    return fields


def _proto_bytes(data: bytes, field_number: int) -> list[bytes]:
    return [value for number, wire, value in _fields(data) if number == field_number and wire == 2 and isinstance(value, bytes)]


def _proto_string(data: bytes, field_number: int) -> str | None:
    values = _proto_bytes(data, field_number)
    if not values:
        return None
    try:
        return values[0].decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("Invalid UTF-8 in AAB manifest") from exc


def aab_package(path: str | Path) -> str:
    """Read the package attribute from the AAB's protobuf manifest."""
    bundle = Path(path)
    if not bundle.is_file() or bundle.suffix.lower() != ".aab":
        raise ReleaseError("A valid .aab file is required")
    try:
        with zipfile.ZipFile(bundle) as archive:
            manifest = archive.read("base/manifest/AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ReleaseError("AAB has no readable base manifest") from exc
    for element in _proto_bytes(manifest, 1):
        if _proto_string(element, 3) != "manifest":
            continue
        for attribute in _proto_bytes(element, 4):
            if _proto_string(attribute, 2) == "package":
                package = _proto_string(attribute, 3)
                if package and PACKAGE_RE.fullmatch(package):
                    return package
    raise ReleaseError("AAB manifest does not contain a valid package")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _google_package_path(package: str) -> str:
    if not PACKAGE_RE.fullmatch(package):
        raise ReleaseError("Invalid Google Play package ID")
    return f"/applications/{package}"


def _existing_releases(body: dict[str, Any]) -> list[dict[str, Any]]:
    releases = body.get("releases", [])
    if not isinstance(releases, list):
        raise ApiError("Google Play returned an invalid track")
    return [release for release in releases if isinstance(release, dict)]


def _release_for(version_code: str, status: str) -> dict[str, Any]:
    if status not in ("draft", "completed"):
        raise ReleaseError("Release status must be draft or completed")
    return {"versionCodes": [str(version_code)], "status": status}


def _track_update(
    releases: list[dict[str, Any]], version_code: str, status: str,
    track: str, replace_track: bool, same_bundle: bool,
) -> tuple[str, list[dict[str, Any]]]:
    wanted = _release_for(version_code, status)
    for existing in releases:
        codes = [str(code) for code in existing.get("versionCodes", [])]
        if str(version_code) in codes:
            if same_bundle and existing.get("status") == status:
                return "already-on-track", releases
            raise ReleaseError("Version code is already on this track with a different bundle or status")
    if track == "production" and releases and not replace_track:
        raise ReleaseError("Production track already has releases; pass --replace-track to replace them")
    if replace_track:
        return "update-track", [wanted]
    # Preserve active/staged releases on other tracks when adding a draft.
    return "update-track", releases + [wanted]


def android_upload(
    config: dict[str, Any], aab: str | Path, *, track: str = "internal",
    status: str = "draft", replace_track: bool = False, dry_run: bool = False,
    client: Any = None,
) -> dict[str, Any]:
    """Validate package identity, upload an AAB, then validate and commit a Play edit."""
    if track not in ("internal", "alpha", "beta", "production"):
        raise ReleaseError("Unsupported Google Play track")
    if status not in ("draft", "completed"):
        raise ReleaseError("Release status must be draft or completed")
    if replace_track and status != "completed":
        raise ReleaseError("--replace-track is only allowed for a completed release")
    if track == "production" and not dry_run:
        require_ready()
    package = require_project_identity(config, "android")
    path = Path(aab)
    actual = aab_package(path)
    if actual != package:
        raise ReleaseError("AAB package does not match release config bundleId")
    digest = _sha256(path)
    result: dict[str, Any] = {
        "platform": "android", "package": package, "track": track,
        "status": status, "aabSha256": digest, "dryRun": dry_run,
    }
    if dry_run:
        result["action"] = "would-upload"
        return result
    client = client or GoogleClient(config)
    base = _google_package_path(package)
    edit = client.request("POST", base + "/edits", json_body={})
    edit_id = _safe_id(str(edit.get("id", "")), "Google Play edit ID")
    edit_base = base + "/edits/" + edit_id
    committed = False
    try:
        bundle_list = client.request("GET", edit_base + "/bundles")
        bundles = bundle_list.get("bundles", [])
        if not isinstance(bundles, list):
            raise ApiError("Google Play returned an invalid bundle list")
        matching = [bundle for bundle in bundles if isinstance(bundle, dict) and str(bundle.get("sha256", "")).lower() == digest]
        same_bundle = bool(matching)
        if matching:
            version_code = str(matching[0].get("versionCode", ""))
        else:
            with path.open("rb") as stream:
                uploaded = client.request("POST", edit_base + "/bundles?uploadType=media", data=stream, upload=True)
            version_code = str(uploaded.get("versionCode", ""))
        if not version_code.isdigit():
            raise ApiError("Google Play did not return an AAB version code")
        track_path = edit_base + "/tracks/" + track
        track_body = client.request("GET", track_path)
        existing = _existing_releases(track_body)
        action, updated = _track_update(existing, version_code, status, track, replace_track, same_bundle)
        result["versionCode"] = int(version_code)
        result["action"] = action
        if action == "already-on-track":
            return result
        client.request("PUT", track_path, json_body={"track": track, "releases": updated})
        client.request("POST", edit_base + ":validate")
        client.request("POST", edit_base + ":commit", params={"changesInReviewBehavior": "ERROR_IF_IN_REVIEW"})
        committed = True
        result["action"] = "committed"
        return result
    finally:
        if not committed:
            try:
                client.request("DELETE", edit_base)
            except ReleaseError:
                pass


def _google_user_summary(body: dict[str, Any], email: str) -> dict[str, Any]:
    users = body.get("users", [])
    if not isinstance(users, list):
        raise ApiError("Google Play returned an invalid users list")
    matched: list[dict[str, Any]] = []
    for user in users:
        if not isinstance(user, dict) or user.get("email") != email:
            continue
        matched.append({
            "email": email,
            "accessState": user.get("accessState"),
            "developerAccountPermissions": user.get("developerAccountPermissions", []),
            "grants": user.get("grants", []),
        })
    return {"serviceAccount": email or None, "userRecords": matched}


def doctor(config: dict[str, Any], *, apple: Any = None, google: Any = None) -> dict[str, Any]:
    """Read-only account, app, bundle-ID, certificate and Play permission checks."""
    apple = apple or AppleClient(config)
    google = google or GoogleClient(config)
    output: dict[str, Any] = {"bundleId": config["bundleId"], "apple": {}, "google": {}}
    apps = apple.all("/v1/apps", params={"limit": "200"})
    output["apple"]["apps"] = [
        {"id": item.get("id"), "bundleId": _attr(item, "bundleId"), "name": _attr(item, "name")}
        for item in apps
    ]
    bundle_ids = apple.all("/v1/bundleIds", params={"limit": "200"})
    output["apple"]["bundleIds"] = [
        {"id": item.get("id"), "identifier": _attr(item, "identifier"), "platform": _attr(item, "platform")}
        for item in bundle_ids
    ]
    certificates = apple.all("/v1/certificates", params={"limit": "200"})
    output["apple"]["certificates"] = [
        {"id": item.get("id"), "name": _attr(item, "name"), "certificateType": _attr(item, "certificateType"),
         "expirationDate": _attr(item, "expirationDate")}
        for item in certificates
    ]
    developer_id = _required_string(config["google"], "developerId", "google")
    _safe_id(developer_id, "Google Play developer ID")
    # The users endpoint is an optional permissions diagnostic. An unavailable
    # list must not discard successful Apple account and Google OAuth checks.
    try:
        users = google.request("GET", f"/developers/{developer_id}/users", params={"pageSize": -1})
        output["google"] = _google_user_summary(users, getattr(google, "service_email", ""))
        output["google"]["usersCheck"] = {"ok": True}
    except ReleaseError as exc:
        output["google"] = {
            "serviceAccount": getattr(google, "service_email", None),
            "userRecords": [],
            "usersCheck": {"ok": False, "error": str(exc)},
        }
    output["google"]["developerId"] = developer_id
    return output


def android_products(config: dict[str, Any], *, client: Any = None) -> dict[str, Any]:
    """Read one-time products through the current publishing API without a Play edit."""
    package = config["bundleId"]
    path = _google_package_path(package) + "/oneTimeProducts"
    client = client or GoogleClient(config)
    products: list[dict[str, Any]] = []
    params: dict[str, Any] = {"pageSize": 100}
    seen_tokens: set[str] = set()
    while True:
        body = client.request("GET", path, params=dict(params))
        page = body.get("oneTimeProducts", [])
        if not isinstance(page, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("productId"), str)
            or not item["productId"].strip()
            for item in page
        ):
            raise ApiError("Google Play returned an invalid one-time products list")
        products.extend(page)
        token = body.get("nextPageToken", "")
        if not isinstance(token, str):
            raise ApiError("Google Play returned an invalid product page token")
        if not token:
            break
        if token in seen_tokens:
            raise ApiError("Google Play returned a repeated product page token")
        seen_tokens.add(token)
        params["pageToken"] = token
    return {"packageName": package, "oneTimeProducts": products}


def status(config: dict[str, Any], *, apple: Any = None, google: Any = None) -> dict[str, Any]:
    """Read current review status without opening a Play edit."""
    apple = apple or AppleClient(config)
    google = google or GoogleClient(config)
    ready, missing = readiness_report()
    output: dict[str, Any] = {
        "bundleId": config["bundleId"],
        "readiness": {"ready": ready, "missing": missing},
        "apple": {}, "google": {"releases": {}},
    }
    app_id = config.get("apple", {}).get("appId")
    if app_id:
        app_id = _apple_app_id(config, str(app_id))
        app = _single_data(apple.request("GET", f"/v1/apps/{app_id}"), "Apple app")
        if _attr(app, "bundleId") != config["bundleId"]:
            raise ReleaseError("Apple app bundle ID does not match release config")
        output["apple"]["appId"] = app_id
        output["apple"]["versions"] = [
            {"id": item.get("id"), "version": _attr(item, "versionString"), "platform": _attr(item, "platform"),
             "state": _attr(item, "appVersionState"), "releaseType": _attr(item, "releaseType")}
            for item in _apple_versions(apple, app_id)
        ]
        output["apple"]["reviewSubmissions"] = [
            {"id": item.get("id"), "state": _attr(item, "state"), "platform": _attr(item, "platform")}
            for item in _apple_submissions(apple, app_id)
        ]
    else:
        output["apple"]["appId"] = None
    package_path = _google_package_path(config["bundleId"])
    for track in ("internal", "alpha", "beta", "production"):
        body = google.request("GET", package_path + f"/tracks/{track}/releases")
        releases = body.get("releases", [])
        if not isinstance(releases, list):
            raise ApiError("Google Play returned an invalid releases list")
        output["google"]["releases"][track] = [
            {"name": item.get("releaseName"), "state": item.get("releaseLifecycleState"),
             "versionCodes": [artifact.get("versionCode") for artifact in item.get("activeArtifacts", [])
                              if isinstance(artifact, dict)]}
            for item in releases if isinstance(item, dict)
        ]
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Local release config JSON (default: RELEASE_CONFIG or .private/release-config.json)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Read-only App Store Connect and Play account checks")
    commands.add_parser("status", help="Read-only app and review state")
    commands.add_parser("android-products", help="Read-only one-time product status using the current Play publishing API")
    apple_parser = commands.add_parser("apple-review", help="Submit a specific iOS version for App Review")
    apple_parser.add_argument("--app-id", required=True)
    apple_parser.add_argument("--build-id", required=True)
    apple_parser.add_argument("--version", required=True)
    apple_parser.add_argument("--dry-run", action="store_true")
    android_parser = commands.add_parser("android-upload", help="Upload an AAB through a Play edit")
    android_parser.add_argument("--aab", required=True)
    android_parser.add_argument("--track", choices=("internal", "alpha", "beta", "production"), default="internal")
    android_parser.add_argument("--status", choices=("draft", "completed"), default="draft")
    android_parser.add_argument("--replace-track", action="store_true")
    android_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            output = doctor(config)
        elif args.command == "status":
            output = status(config)
        elif args.command == "android-products":
            output = android_products(config)
        elif args.command == "apple-review":
            output = apple_review(config, args.app_id, args.build_id, args.version, dry_run=args.dry_run)
        else:
            output = android_upload(config, args.aab, track=args.track, status=args.status,
                                    replace_track=args.replace_track, dry_run=args.dry_run)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except ReleaseError as exc:
        print(f"Release error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Unexpected release error ({type(exc).__name__})", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
