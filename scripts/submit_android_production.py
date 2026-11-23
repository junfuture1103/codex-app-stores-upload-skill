#!/usr/bin/env python3
"""Upload an AAB and set/replace the Google Play *production* release, then commit.

Unlike store_release.android_upload (which enforces readiness.json via
require_ready() and commits with ERROR_IF_IN_REVIEW), this helper performs a
plain commit so a new build can SUPERSEDE a build that is already in review —
the exact case when a prior build must be replaced before approval.

Usage:
    python3 scripts/submit_android_production.py release-artifacts/app-android-build25.aab \
        --version-code 25 --version-name 1.0.2 --metadata release/metadata/ko.json \
        --screenshots-dir release-artifacts/screenshots-android-build25 --dry-run

Credentials: read via store_release.load_config (RELEASE_CONFIG env or
.private/release-config.json, fallback handoff/credentials/release-config.json).
--metadata and --screenshots-dir update the ko-KR listing in the SAME edit as
the release. --dry-run validates local inputs without creating an edit or
uploading anything. Existing consumable products/prices are never modified.
See release/STORE_AUTOMATION_HANDOFF.md.
"""
import argparse
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.store_release import (GoogleClient, load_config, _sha256, aab_package,
                                  _proto_bytes, _proto_string, ReleaseError, ApiError)  # noqa: E402
from scripts.update_release_assets import replace_google_images, image_list  # noqa: E402


def aab_identity(path: Path) -> dict[str, str]:
    """Read identity from the signed bundle, reusing the existing protobuf reader.

    Schema: AOSP tools/aapt2/Resources.proto (XmlNode/XmlElement/XmlAttribute).
    Fail closed if the required literal version attributes are unavailable.
    """
    package = aab_package(path)
    with zipfile.ZipFile(path) as archive:
        manifest = archive.read("base/manifest/AndroidManifest.xml")
    versions = {}
    for element in _proto_bytes(manifest, 1):
        if _proto_string(element, 3) != "manifest":
            continue
        for attribute in _proto_bytes(element, 4):
            name = _proto_string(attribute, 2)
            if name in {"versionCode", "versionName"} and _proto_string(attribute, 1) == "http://schemas.android.com/apk/res/android":
                if name in versions:
                    raise ReleaseError(f"AAB has duplicate {name} attributes")
                versions[name] = _proto_string(attribute, 3)
    if not (versions.get("versionCode") or "").isdigit() or int(versions["versionCode"]) <= 0:
        raise ReleaseError("AAB does not contain a valid literal versionCode")
    if not versions.get("versionName"):
        raise ReleaseError("AAB does not contain a literal versionName")
    return {"package": package, **versions}


def load_listing(path: str | Path | None) -> dict | None:
    if path is None:
        return None
    metadata = json.loads(Path(path).read_text())
    listing = {"language": "ko-KR", "title": metadata["app"]["name"],
               "shortDescription": metadata["googlePlay"]["shortDescription"],
               "fullDescription": metadata["googlePlay"]["fullDescription"]}
    for field, limit in [("title", 30), ("shortDescription", 80), ("fullDescription", 4000)]:
        if not isinstance(listing[field], str) or not 1 <= len(listing[field]) <= limit:
            raise ReleaseError(f"Google listing {field} must contain 1–{limit} characters")
    return listing


def submit_production(config: dict, aab: str | Path, *, version_code: str, version_name: str,
                      notes_ko: str = "", metadata: str | Path | None = None,
                      screenshots_dir: str | Path | None = None, dry_run: bool = False,
                      client: GoogleClient | None = None) -> dict:
    aab = Path(aab)
    identity = aab_identity(aab)
    expected = {"package": config["bundleId"], "versionCode": str(version_code), "versionName": version_name}
    if identity != expected:
        raise ReleaseError(f"AAB identity mismatch: actual={identity}, expected={expected}")
    listing = load_listing(metadata)
    paths = []
    if screenshots_dir is not None:
        directory = Path(screenshots_dir)
        if not directory.is_dir():
            raise ReleaseError(f"Screenshot directory does not exist: {directory}")
        paths = sorted(directory.glob("*.png"))
        if len(paths) < 2:
            raise ReleaseError("Play phone screenshots require at least two PNG files")
        image_list(paths, 8)  # Validate all files before the first remote mutation.
    if len(notes_ko) > 500:
        raise ReleaseError("Korean release notes must not exceed 500 characters")
    result = {"identity": identity, "aabSha256": _sha256(aab), "dryRun": dry_run,
              "listingLanguage": "ko-KR" if listing else None,
              "screenshotFiles": [str(p) for p in paths], "committed": False}
    if dry_run:
        result["action"] = "would-upload-update-listing-images-and-commit-production"
        return result

    pkg = identity["package"]
    g = client or GoogleClient(config)
    base = f"/applications/{pkg}"
    digest = result["aabSha256"]
    edit = g.request("POST", base + "/edits", json_body={})
    eid = edit["id"]
    eb = f"{base}/edits/{eid}"
    committed = False
    try:
        bundles = g.request("GET", eb + "/bundles").get("bundles", [])
        match = [b for b in bundles if str(b.get("sha256", "")).lower() == digest]
        if match:
            vc = str(match[0]["versionCode"])
            print(f"bundle already uploaded, versionCode {vc}")
        else:
            with aab.open("rb") as fh:
                up = g.request("POST", eb + "/bundles?uploadType=media", data=fh, upload=True)
            vc = str(up["versionCode"])
            print(f"uploaded, versionCode {vc}")
        if vc != str(version_code):
            raise ReleaseError(f"Uploaded versionCode mismatch: AAB={vc}, expected {version_code}")

        if listing:
            listing_path = eb + "/listings/ko-KR"
            try:
                current = g.request("GET", listing_path)
            except ApiError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                current = {}
            updated = {key: current[key] for key in ("language", "title", "shortDescription", "fullDescription", "video") if key in current}
            updated.update(listing)
            g.request("PUT", listing_path, json_body=updated)
            readback = g.request("GET", listing_path)
            if any(readback.get(key) != value for key, value in listing.items()):
                raise ReleaseError("Google listing readback mismatch; edit will be discarded")
        if paths:
            replace_google_images(g, pkg, eid, "ko-KR", "phoneScreenshots", paths, apply=True)

        release = {
            "name": f"{version_name} ({vc})",
            "versionCodes": [vc],
            "status": "completed",
        }
        if notes_ko:
            release["releaseNotes"] = [{"language": "ko-KR", "text": notes_ko}]
        g.request("PUT", eb + "/tracks/production", json_body={"track": "production", "releases": [release]})
        g.request("POST", eb + ":validate")
        g.request("POST", eb + ":commit")  # plain commit: sends for review, supersedes any in-review build
        committed = True
        result.update(committed=True, editId=eid, release=release)
        return result
    finally:
        if not committed:
            try:
                g.request("DELETE", eb)
                print("edit discarded")
            except Exception as exc:  # noqa: BLE001
                print("discard error:", str(exc)[:100])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("aab")
    ap.add_argument("--version-code", required=True)
    ap.add_argument("--version-name", default="1.0.1")
    ap.add_argument("--notes-ko", default="")
    ap.add_argument("--metadata", help="Store metadata JSON; app.name and googlePlay descriptions")
    ap.add_argument("--screenshots-dir", help="Reviewed PNGs in lexical filename order")
    ap.add_argument("--dry-run", action="store_true", help="Validate local files only; no API calls or uploads")
    args = ap.parse_args(argv)
    if not os.environ.get("RELEASE_CONFIG"):
        default = ROOT / ".private" / "release-config.json"
        fallback = ROOT / "handoff" / "credentials" / "release-config.json"
        os.environ["RELEASE_CONFIG"] = str(default if default.exists() else fallback)
    try:
        result = submit_production(load_config(), args.aab, version_code=args.version_code,
            version_name=args.version_name, notes_ko=args.notes_ko, metadata=args.metadata,
            screenshots_dir=args.screenshots_dir, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, ApiError, OSError, KeyError, ValueError) as exc:
        print(f"Production release failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
