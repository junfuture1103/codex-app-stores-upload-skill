#!/usr/bin/env python3
"""Preflight for the store-upload skill.

Validates that the signing/API credentials and local toolchain are ready BEFORE a
build is attempted, so a new user only has to fill in `.private/release-config.json`
and run this once. Checks nothing destructive — read-only.

    python3 .claude/skills/store-upload/preflight.py            # both platforms
    python3 .claude/skills/store-upload/preflight.py --ios      # iOS only
    python3 .claude/skills/store-upload/preflight.py --android  # Android only

Exit code 0 = ready, 1 = something is missing (details printed).
Config is read from $RELEASE_CONFIG, else .private/release-config.json, else
handoff/credentials/release-config.json.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (.claude/skills/store-upload/ -> up 3)
PLACEHOLDERS = {"REQUIRED", "", None, "REPLACE_ME", "TODO"}
ok_count = 0
fail_count = 0


def line(ok, label, detail=""):
    global ok_count, fail_count
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    if ok:
        ok_count += 1
    else:
        fail_count += 1
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def find_config():
    for p in [os.environ.get("RELEASE_CONFIG"),
              ROOT / ".private" / "release-config.json",
              ROOT / "handoff" / "credentials" / "release-config.json"]:
        if p and Path(p).is_file():
            return Path(p)
    return None


def req_str(obj, key):
    v = obj.get(key) if isinstance(obj, dict) else None
    return isinstance(v, str) and v.strip() and v.strip() not in PLACEHOLDERS


def req_file(obj, key, base):
    v = obj.get(key) if isinstance(obj, dict) else None
    if not (isinstance(v, str) and v.strip() and v.strip() not in PLACEHOLDERS):
        return False, "값 없음"
    path = Path(v)
    if not path.is_absolute():
        path = base / path
    return (path.is_file(), str(path) if path.is_file() else f"파일 없음: {path}")


def check_config(want_ios, want_android):
    print("\n[1/3] 크리덴셜 설정 (release-config.json)")
    cfg_path = find_config()
    if not cfg_path:
        line(False, "release-config.json", "없음 — .private/release-config.json 에 생성하세요 (release-config.example.json 참고)")
        return False
    line(True, "설정 파일", str(cfg_path))
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as e:  # noqa: BLE001
        line(False, "JSON 파싱", str(e)[:80])
        return False
    base = cfg_path.parent
    line(req_str(cfg, "bundleId"), "bundleId", cfg.get("bundleId", ""))
    # app.json 의 번들ID와 일치하는지
    try:
        app = json.loads((ROOT / "app.json").read_text())["expo"]
        ios_pkg = (app.get("ios") or {}).get("bundleIdentifier")
        aos_pkg = (app.get("android") or {}).get("package")
        if cfg.get("bundleId"):
            if want_ios and ios_pkg:
                line(ios_pkg == cfg["bundleId"], "app.json ios.bundleIdentifier == bundleId", f"{ios_pkg}")
            if want_android and aos_pkg:
                line(aos_pkg == cfg["bundleId"], "app.json android.package == bundleId", f"{aos_pkg}")
    except Exception:  # noqa: BLE001
        pass

    if want_ios:
        a = cfg.get("apple", {})
        for k in ("keyId", "issuerId", "teamId", "p12Password"):
            line(req_str(a, k), f"apple.{k}")
        line(req_str(a, "appId"), "apple.appId", "(심사 제출 시 필요 · apps.apple.com/.../id<이값>)")
        for k in ("keyPath", "p12Path", "profilePath"):
            o, d = req_file(a, k, base)
            line(o, f"apple.{k}", d)
    if want_android:
        g = cfg.get("google", {})
        o, d = req_file(g, "serviceAccountPath", base)
        line(o, "google.serviceAccountPath", d)
        an = cfg.get("android", {})
        for k in ("storePassword", "keyAlias", "keyPassword"):
            line(req_str(an, k), f"android.{k}")
        o, d = req_file(an, "keystorePath", base)
        line(o, "android.keystorePath", d)
    return True


def check_toolchain(want_ios, want_android):
    print("\n[2/3] 공통 툴체인")
    line(bool(shutil.which("node")), "node", _ver("node", "--version"))
    line(bool(shutil.which("pnpm")), "pnpm", _ver("pnpm", "--version"))
    line((ROOT / "node_modules").is_dir(), "node_modules", "pnpm install 필요" if not (ROOT / "node_modules").is_dir() else "")
    for mod in ("cryptography", "requests"):
        try:
            __import__(mod)
            line(True, f"python: {mod}")
        except Exception:  # noqa: BLE001
            line(False, f"python: {mod}", "pip install -r release/requirements.txt")

    if want_android:
        print("\n[2b] Android 툴체인")
        jh = os.environ.get("JAVA_HOME") or _java_home_17()
        line(bool(jh), "JDK 17 (JAVA_HOME)", jh or "set JAVA_HOME (openjdk@17). build 스크립트가 /opt/homebrew/opt/openjdk@17 로 폴백")
        sdk = os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Library/Android/sdk")
        line(Path(sdk).is_dir(), "Android SDK", sdk)
    if want_ios:
        print("\n[2c] iOS 툴체인 (macOS 전용)")
        is_mac = sys.platform == "darwin"
        line(is_mac, "macOS", sys.platform)
        line(bool(shutil.which("xcodebuild")), "Xcode (xcodebuild)", _ver("xcodebuild", "-version").split("\n")[0] if shutil.which("xcodebuild") else "App Store에서 Xcode 설치")


def _ver(cmd, *flag):
    try:
        return subprocess.run([cmd, *flag], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _java_home_17():
    try:
        out = subprocess.run(["/usr/libexec/java_home", "-v", "17"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    fallback = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    return fallback if Path(fallback).is_dir() else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ios", action="store_true", help="iOS만 검사")
    ap.add_argument("--android", action="store_true", help="Android만 검사")
    args = ap.parse_args()
    want_ios = args.ios or not args.android
    want_android = args.android or not args.ios

    print("=== store-upload preflight ===")
    print(f"repo: {ROOT}")
    check_config(want_ios, want_android)
    check_toolchain(want_ios, want_android)
    print(f"\n[3/3] 결과: {ok_count} OK, {fail_count} 실패")
    if fail_count:
        print("→ 위 ✗ 항목을 먼저 채우세요. 크리덴셜 발급법은 CREDENTIALS.md 참고.")
        sys.exit(1)
    print("→ 준비 완료. SKILL.md 의 실행 명령으로 업로드하세요.")


if __name__ == "__main__":
    main()
