#!/usr/bin/env bash
# iOS 원커맨드 빌드 + App Store Connect 업로드 (+심사 제출). 앱 비종속 — store-upload 스킬.
#
# scripts/ios_release.py 의 release 가 서명 p12 재암호화 → build_ios.sh 빌드 →
# altool 업로드 → 빌드 처리 대기 → (--submit) 심사 제출까지 전부 처리합니다.
# 웹/AOS와 같은 공유 RN 코드(App.tsx)라, 최신 기능은 빌드만 하면 그대로 반영됩니다.
# 서명/API 자격은 .private/release-config.json 에서 읽습니다
# (크리덴셜 설정: .claude/skills/store-upload/CREDENTIALS.md, 사전검증 preflight.py).
#
# 사용법:
#   bash ios-upload/auto-upload.sh             # 빌드 + 업로드 (심사 제출은 안 함)
#   bash ios-upload/auto-upload.sh --submit    # 빌드 + 업로드 + 심사 제출
#   bash ios-upload/auto-upload.sh --build-number 30
#   bash ios-upload/auto-upload.sh --resume --ipa release-artifacts/app-ios-build30.ipa
#
# 전제: macOS + Xcode, pnpm, node_modules, python deps(release/requirements.txt),
#   서명 설정 .private/release-config.json(또는 handoff/credentials/release-config.json)
#   의 apple.{teamId,keyId,issuerId,keyPath,p12Path,p12Password,profilePath,appId}.
#   ⚠️ --submit 은 App Store Connect에 app.json version(현재 1.0.3) 버전이 미리
#   생성돼 있어야 합니다(없으면 ASC에서 + 버튼으로 새 버전 1.0.3 먼저 생성).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ "$(uname -s)" = Darwin ] || { echo "[ios] iOS 빌드는 macOS + Xcode가 필요합니다."; exit 1; }
command -v xcodebuild >/dev/null || { echo "[ios] Xcode(xcodebuild)가 필요합니다."; exit 1; }

SUBMIT=0; BN=auto; IPA=""; RESUME=0; NOTES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --submit) SUBMIT=1; shift;;
    --build-number) BN="$2"; shift 2;;
    --ipa) IPA="$2"; shift 2;;
    --resume) RESUME=1; shift;;
    --notes) NOTES="$2"; shift 2;;   # resolution-notes file (required when resubmitting after a rejection)
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "[ios] unknown arg: $1" >&2; exit 2;;
  esac
done

export RELEASE_CONFIG="${RELEASE_CONFIG:-$ROOT/.private/release-config.json}"
[ -f "$RELEASE_CONFIG" ] || export RELEASE_CONFIG="$ROOT/handoff/credentials/release-config.json"
[ -f "$RELEASE_CONFIG" ] || { echo "[ios] 서명/업로드 설정을 찾을 수 없습니다 (RELEASE_CONFIG)"; exit 2; }

# release가 버전 자동 산출 + p12 재암호화 + build_ios.sh + altool 업로드 + (옵션)심사 제출.
args=(release --build-number "$BN")
[ -n "$IPA" ] && args+=(--ipa "$IPA")
[ "$RESUME" = 1 ] && args+=(--skip-upload)   # --skip-upload 은 반드시 --ipa 와 함께
[ "$SUBMIT" = 1 ] && args+=(--submit)
[ -n "$NOTES" ] && args+=(--resolution-notes-file "$NOTES")

echo "[ios] python3 scripts/ios_release.py ${args[*]}"
python3 scripts/ios_release.py "${args[@]}"
echo "[ios] 완료 (submit=$SUBMIT)."
echo "[ios] 상태 확인: RELEASE_CONFIG=$RELEASE_CONFIG python3 scripts/ios_release.py status"
