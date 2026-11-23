#!/usr/bin/env bash
# Android(AOS) 원커맨드 빌드 + Google Play 업로드 (앱 비종속 — store-upload 스킬).
#
# 명령 한 번으로 현재 HEAD(공유 React Native 코드 = 웹과 동일 기능)를 서명 AAB로
# 빌드하고 Google Play에 올립니다. versionCode는 자동으로 (Play 최고치 + 1).
# 서명/업로드 자격은 .private/release-config.json 에서 읽습니다(크리덴셜 설정은
# .claude/skills/store-upload/CREDENTIALS.md, 사전검증 preflight.py).
#
# 사용법:
#   bash aos/auto-upload.sh                     # 프로덕션, versionCode 자동, versionName=app.json
#   bash aos/auto-upload.sh --dry-run           # 빌드 + 검증만 (업로드 안 함)
#   bash aos/auto-upload.sh --track internal    # 내부 테스트 트랙(결제 실기기 테스트용)
#   bash aos/auto-upload.sh --version-name 1.0.4
#   bash aos/auto-upload.sh --version-code 30
#   bash aos/auto-upload.sh --notes "릴리스 노트 ..."
#
# 전제: Android SDK(~/Library/Android/sdk), JDK17, pnpm, node_modules, 서명 설정
#   (.private/release-config.json 또는 handoff/credentials/release-config.json),
#   python deps(release/requirements.txt: cryptography, requests).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TRACK=production; VC=""; VN=""; NOTES=""; DRYRUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --track) TRACK="$2"; shift 2;;
    --version-code) VC="$2"; shift 2;;
    --version-name) VN="$2"; shift 2;;
    --notes) NOTES="$2"; shift 2;;
    --dry-run) DRYRUN=1; shift;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "[aos] unknown arg: $1" >&2; exit 2;;
  esac
done

case "$TRACK" in production|internal|alpha|beta) ;; *) echo "[aos] bad --track: $TRACK" >&2; exit 2;; esac

export RELEASE_CONFIG="${RELEASE_CONFIG:-$ROOT/.private/release-config.json}"
[ -f "$RELEASE_CONFIG" ] || export RELEASE_CONFIG="$ROOT/handoff/credentials/release-config.json"
[ -f "$RELEASE_CONFIG" ] || { echo "[aos] 서명/업로드 설정을 찾을 수 없습니다 (RELEASE_CONFIG)"; exit 2; }

# versionName 기본값 = app.json
[ -n "$VN" ] || VN="$(python3 -c "import json; print(json.load(open('app.json'))['expo']['version'])")"

# versionCode 자동 = Play 최고치 + 1
if [ -z "$VC" ]; then
  echo "[aos] Play에서 다음 versionCode 조회 중 ..."
  VC="$(python3 "$ROOT/aos/next-version-code.py")"
fi
[[ "$VC" =~ ^[0-9]+$ ]] || { echo "[aos] versionCode 계산 실패: '$VC'"; exit 1; }
echo "[aos] versionCode=$VC  versionName=$VN  track=$TRACK  dryRun=$DRYRUN"

# 1) 서명 AAB 빌드 (최신 HEAD 전 기능 포함, FCM 연결)
# APP_PUSH=0 으로 푸시 비활성 가능(기본 1=google-services.json 있으면 FCM 연결).
APP_BUILD_NUMBER="$VC" APP_PUSH="${APP_PUSH:-${APP_PUSH:-1}}" bash scripts/build_android.sh
AAB="release-artifacts/app-android-build${VC}.aab"
[ -f "$AAB" ] || { echo "[aos] 빌드 산출물 없음: $AAB"; exit 1; }

# 2) 업로드
[ -n "$NOTES" ] || NOTES="최신 개선 사항을 반영했습니다."
if [ "$TRACK" = "production" ]; then
  # 프로덕션: 심사중 빌드를 교체 가능한 plain commit (readiness 게이트 우회)
  args=(--version-code "$VC" --version-name "$VN" --notes-ko "$NOTES")
  [ "$DRYRUN" = 1 ] && args+=(--dry-run)
  python3 scripts/submit_android_production.py "$AAB" "${args[@]}"
else
  # internal/alpha/beta: 테스트 트랙 (결제 실기기 검증용)
  args=(android-upload --aab "$AAB" --track "$TRACK" --status completed)
  [ "$DRYRUN" = 1 ] && args+=(--dry-run)
  python3 scripts/store_release.py "${args[@]}"
fi

echo "[aos] 완료 (track=$TRACK, versionCode=$VC, versionName=$VN)"
echo "[aos] 상태 확인: RELEASE_CONFIG=$RELEASE_CONFIG python3 scripts/store_release.py status"
