#!/usr/bin/env python3
"""Deep validator for the actual store credential FILES.

preflight.py checks that fields/files are present. This goes further: it opens
each secret and verifies it is the RIGHT, VALID, UNEXPIRED file and that the
pieces match each other (cert team == teamId, profile bundle == bundleId,
keystore has the alias, etc.). Read-only; prints secrets' metadata, never secrets.

    python3 .claude/skills/store-upload/check-credentials.py
    python3 .claude/skills/store-upload/check-credentials.py --ios      # Apple files only
    python3 .claude/skills/store-upload/check-credentials.py --android  # Google/keystore only

Exit 0 = all valid, 1 = a problem (details printed). Config resolved like preflight
(RELEASE_CONFIG → .private/release-config.json → handoff/credentials/...).
"""
import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLACEHOLDERS = {"REQUIRED", "", None, "REPLACE_ME", "TODO"}
ok = warn = bad = 0
NOW = dt.datetime.now(dt.timezone.utc)


def P(status, label, detail=""):
    global ok, warn, bad
    m = {"ok": "\033[32m✓\033[0m", "warn": "\033[33m⚠\033[0m", "bad": "\033[31m✗\033[0m"}[status]
    if status == "ok":
        ok += 1
    elif status == "warn":
        warn += 1
    else:
        bad += 1
    print(f"  {m} {label}" + (f" — {detail}" if detail else ""))


def find_config():
    for p in [os.environ.get("RELEASE_CONFIG"),
              ROOT / ".private" / "release-config.json",
              ROOT / "handoff" / "credentials" / "release-config.json"]:
        if p and Path(p).is_file():
            return Path(p)
    return None


def resolve(val, base):
    if not (isinstance(val, str) and val.strip() and val.strip() not in PLACEHOLDERS):
        return None
    p = Path(val)
    return p if p.is_absolute() else base / p


def days(exp):
    return (exp - NOW).days


def expiry_status(exp, what):
    d = days(exp)
    s = exp.strftime("%Y-%m-%d")
    if d < 0:
        P("bad", f"{what} 만료됨", f"{s} ({-d}일 지남)")
    elif d < 30:
        P("warn", f"{what} 곧 만료", f"{s} ({d}일 남음)")
    else:
        P("ok", f"{what} 유효", f"{s} ({d}일 남음)")


# ---------------- Apple ----------------
def check_p8(a, base):
    p = resolve(a.get("keyPath"), base)
    if not p or not p.is_file():
        P("bad", "apple.keyPath (.p8)", f"파일 없음: {p}"); return
    txt = p.read_text(errors="ignore")
    if "BEGIN PRIVATE KEY" not in txt:
        P("bad", "apple .p8 형식", "PEM private key가 아님(ASC API 키 .p8 맞는지 확인)"); return
    fn = p.name
    m = re.search(r"AuthKey_([A-Z0-9]{10})", fn)
    if m and a.get("keyId") and m.group(1) != a.get("keyId"):
        P("warn", "apple .p8 파일명 keyId", f"파일 {m.group(1)} ≠ config keyId {a.get('keyId')}")
    else:
        P("ok", "apple .p8 (ASC API 키)", fn)


def check_p12(a, base):
    p = resolve(a.get("p12Path"), base)
    if not p or not p.is_file():
        P("bad", "apple.p12Path (배포 인증서)", f"파일 없음: {p}"); return
    pw = (a.get("p12Password") or "")
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        key, cert, chain = pkcs12.load_key_and_certificates(p.read_bytes(), pw.encode())
    except Exception as e:  # noqa: BLE001
        # 레거시 암호화면 cryptography가 거부 → openssl -legacy 로 재시도
        if shutil.which("openssl"):
            r = subprocess.run(["openssl", "pkcs12", "-in", str(p), "-nokeys", "-passin", f"pass:{pw}", "-legacy"],
                               capture_output=True, text=True)
            if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
                P("warn", "apple .p12 (레거시 암호화)", "openssl -legacy 로만 열림 — ios_release.py가 자동 재암호화함")
                return
        P("bad", "apple .p12 열기 실패", "비밀번호(p12Password) 또는 파일 오류")
        return
    if cert is None:
        P("bad", "apple .p12 인증서 없음", "키만 있고 인증서 없음"); return
    cn = next((x.value for x in cert.subject if x.oid._name == "commonName"), "?")
    team_in_cert = next((x.value for x in cert.subject if x.oid._name in ("organizationalUnitName",)), None)
    if "Distribution" not in str(cn):
        P("warn", "apple .p12 인증서 종류", f"'{cn}' — 'Apple/iPhone Distribution' 아님")
    else:
        P("ok", "apple .p12 배포 인증서", str(cn))
    if a.get("teamId") and team_in_cert and team_in_cert != a["teamId"]:
        P("bad", "apple .p12 ↔ teamId 불일치", f"cert OU {team_in_cert} ≠ {a['teamId']}")
    try:
        expiry_status(cert.not_valid_after_utc, "apple .p12 인증서")
    except AttributeError:
        exp = cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
        expiry_status(exp, "apple .p12 인증서")


def read_mobileprovision(p):
    # macOS security 우선, 아니면 바이트에서 평문 plist 추출.
    if shutil.which("security"):
        r = subprocess.run(["security", "cms", "-D", "-i", str(p)], capture_output=True)
        if r.returncode == 0 and r.stdout:
            try:
                return plistlib.loads(r.stdout)
            except Exception:  # noqa: BLE001
                pass
    data = p.read_bytes()
    i, j = data.find(b"<plist"), data.find(b"</plist>")
    if i >= 0 and j > i:
        try:
            return plistlib.loads(data[i:j + 8])
        except Exception:  # noqa: BLE001
            return None
    return None


def check_profile(a, base, bundle_id):
    p = resolve(a.get("profilePath"), base)
    if not p or not p.is_file():
        P("bad", "apple.profilePath (프로비저닝)", f"파일 없음: {p}"); return
    pl = read_mobileprovision(p)
    if not pl:
        P("bad", "apple .mobileprovision 파싱 실패", "손상되었거나 프로비저닝 파일이 아님"); return
    name = pl.get("Name", "?")
    P("ok", "apple .mobileprovision", name)
    teams = pl.get("TeamIdentifier", [])
    if a.get("teamId") and teams and a["teamId"] not in teams:
        P("bad", "프로파일 ↔ teamId 불일치", f"{teams} ≠ {a['teamId']}")
    appid = (pl.get("Entitlements", {}) or {}).get("application-identifier", "")
    if bundle_id:
        if appid.endswith(bundle_id) or appid.endswith(".*"):
            P("ok", "프로파일 ↔ bundleId", appid)
        else:
            P("bad", "프로파일 ↔ bundleId 불일치", f"{appid} vs {bundle_id}")
    aps = (pl.get("Entitlements", {}) or {}).get("aps-environment")
    P("ok", "프로파일 Push(aps-environment)", aps or "없음(푸시 미사용이면 정상)")
    exp = pl.get("ExpirationDate")
    if isinstance(exp, dt.datetime):
        expiry_status(exp if exp.tzinfo else exp.replace(tzinfo=dt.timezone.utc), "프로비저닝")
    # App Store 배포 프로파일은 ProvisionedDevices가 없어야 함(없으면 배포용)
    if pl.get("ProvisionedDevices"):
        P("warn", "프로파일 종류", "기기 목록 있음 — Ad-hoc/Development일 수 있음(App Store 배포용 아님)")


# ---------------- Google / Android ----------------
def check_service_account(g, base):
    p = resolve(g.get("serviceAccountPath"), base)
    if not p or not p.is_file():
        P("bad", "google.serviceAccountPath", f"파일 없음: {p}"); return
    try:
        sa = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        P("bad", "서비스계정 JSON 파싱", str(e)[:70]); return
    if sa.get("type") != "service_account":
        P("bad", "서비스계정 type", f"'{sa.get('type')}' — service_account 아님"); return
    for k in ("client_email", "private_key", "project_id"):
        if not sa.get(k):
            P("bad", f"서비스계정 {k}", "누락"); return
    P("ok", "google 서비스계정 JSON", sa["client_email"])
    print(f"      ↳ Play Console에서 이 이메일에 '릴리스 관리' 권한 부여 필요: {sa['client_email']}")


def check_keystore(an, base):
    p = resolve(an.get("keystorePath"), base)
    if not p or not p.is_file():
        P("bad", "android.keystorePath", f"파일 없음: {p}"); return
    if not shutil.which("keytool"):
        P("warn", "keytool 없음", "JDK 설치 필요 — keystore 심층검증 건너뜀"); return
    alias = an.get("keyAlias") or ""
    pw = an.get("storePassword") or ""
    # 영어 로케일 강제(한국어 등 비영어 JDK는 "별칭 이름:" 처럼 라벨이 번역돼 파싱이 깨짐).
    en = ["-J-Duser.language=en", "-J-Duser.country=US"]
    r = subprocess.run(["keytool", *en, "-list", "-v", "-keystore", str(p), "-storepass", pw],
                       capture_output=True, text=True)
    if r.returncode != 0:
        P("bad", "keystore 열기 실패", "storePassword 또는 파일 오류"); return
    out = r.stdout
    aliases = [x.strip() for x in re.findall(r"Alias name:\s*(.+)", out)]
    if alias:
        # 라벨 파싱 실패(로케일)도 대비: -alias 존재 확인을 2차 근거로.
        chk = subprocess.run(["keytool", *en, "-list", "-keystore", str(p), "-storepass", pw, "-alias", alias],
                             capture_output=True, text=True)
        if alias in aliases or chk.returncode == 0:
            P("ok", "android keystore + alias", f"{p.name} [{alias}]")
        else:
            P("bad", "keystore alias 불일치", f"config '{alias}' ∉ {aliases or '(파싱 실패)'}")
    else:
        P("ok", "android keystore", f"{p.name} {aliases}")
    sha = re.search(r"SHA256:\s*([0-9A-F:]+)", out) or re.search(r"SHA1:\s*([0-9A-F:]+)", out)
    if sha:
        print(f"      ↳ 업로드 키 지문: {sha.group(1)}  (Play에 등록된 업로드 키와 같아야 함)")
    m = re.search(r"until:\s*(.+)", out)
    if m:
        P("ok", "keystore 유효기간", m.group(1).strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ios", action="store_true")
    ap.add_argument("--android", action="store_true")
    args = ap.parse_args()
    want_ios = args.ios or not args.android
    want_android = args.android or not args.ios

    print("=== store-upload: 크리덴셜 파일 심층 검증 ===")
    cfg_path = find_config()
    if not cfg_path:
        print("  ✗ release-config.json 없음 — 먼저 .private/release-config.json 생성")
        sys.exit(1)
    print(f"config: {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    bundle_id = cfg.get("bundleId") if cfg.get("bundleId") not in PLACEHOLDERS else None

    if want_ios:
        print("\n🍎 Apple (App Store)")
        a = cfg.get("apple", {})
        check_p8(a, base)
        check_p12(a, base)
        check_profile(a, base, bundle_id)
    if want_android:
        print("\n🤖 Google Play + 서명 키")
        check_service_account(cfg.get("google", {}), base)
        check_keystore(cfg.get("android", {}), base)

    print(f"\n결과: {ok} OK · {warn} 경고 · {bad} 실패")
    if bad:
        print("→ ✗ 항목을 고치세요. 발급법은 CREDENTIALS.md 참고.")
        sys.exit(1)
    if warn:
        print("→ 업로드는 가능하지만 ⚠ 항목을 확인하세요(만료 임박/종류 등).")
    else:
        print("→ 모든 크리덴셜 파일이 유효합니다. 업로드 가능 ✦")


if __name__ == "__main__":
    main()
