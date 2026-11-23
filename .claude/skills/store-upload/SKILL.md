---
name: store-upload
description: Build and upload an Expo / React Native app to the Apple App Store and/or Google Play from one credential file. Use when the user asks to ship/release/deploy the mobile app, upload a new store/TestFlight/internal build, submit for review, or "스토어 업로드"/"앱 배포"/"iOS 배포"/"AOS 배포". The user supplies signing + API credentials in .private/release-config.json; everything else (versionCode/build number, signing, submit) is automatic. Not app-specific — works for any Expo project with app.json + the build scripts.
---

# Store upload (App Store + Google Play)

Builds a signed binary from the current HEAD and uploads it to the stores. iOS,
Android, and web ship from the same React Native source, so a new build carries
every current feature automatically.

**This skill is credential-driven.** A new user fills in ONE file
(`.private/release-config.json`) and runs the commands below — no code edits, no
hard-coded app IDs. All store identifiers come from that config and `app.json`.

## Setup (once per machine/app)

1. Create the credential file and fill it in (see **CREDENTIALS.md** for where each value comes from):
   ```bash
   mkdir -p .private
   cp .claude/skills/store-upload/release-config.example.json .private/release-config.json
   # edit .private/release-config.json — replace every "REQUIRED"
   ```
2. Verify it's complete + the toolchain is ready:
   ```bash
   python3 .claude/skills/store-upload/preflight.py           # both platforms
   python3 .claude/skills/store-upload/preflight.py --android  # one platform
   ```
   Fix every ✗ before building. (You can also point to a config elsewhere with
   `RELEASE_CONFIG=/path/to/config.json`.)

## Run it

### Android → Google Play
```bash
bash aos/auto-upload.sh                      # production, versionCode auto (= Play max + 1), versionName from app.json
bash aos/auto-upload.sh --track internal     # internal test track (on-device IAP testing)
bash aos/auto-upload.sh --dry-run            # build + validate only, no upload
bash aos/auto-upload.sh --notes "릴리스 노트"  # Korean release notes
bash aos/auto-upload.sh --version-name 1.2.0 --version-code 40   # pin either
```
~3 min (prebuild + gradle). Wait for `[aos] 완료`. Production commit is sent to
review and supersedes any in-review build.

### iOS → App Store Connect (macOS + Xcode)
```bash
bash ios-upload/auto-upload.sh               # build + upload to TestFlight (no review submit)
bash ios-upload/auto-upload.sh --submit      # build + upload + submit for App Review
bash ios-upload/auto-upload.sh --build-number 40            # pin build number (default auto = latest+1)
bash ios-upload/auto-upload.sh --resume --ipa release-artifacts/<built>.ipa   # re-upload a built IPA (no rebuild)
```
~10–20 min (archive). Wait for `[ios] 완료`. The p12 is auto re-encrypted to a
macOS-importable form. `--submit` needs the `app.json` version to exist as an App
Store version first (create it in ASC, or it is reused if present).

## What it does (engine)
- **Android**: `scripts/build_android.sh` (signed AAB, FCM wired via `APP_PUSH`/`APP_PUSH=1` when `google-services.json` is present) → `scripts/submit_android_production.py` (production) or `scripts/store_release.py android-upload` (test tracks).
- **iOS**: `scripts/ios_release.py release` → p12 re-encrypt → `scripts/build_ios.sh` (archive + export) → altool upload → wait for VALID → optional review submit.
- Build/version numbers: Android versionCode = Play max + 1 (`aos/next-version-code.py`); iOS build = latest ASC build + 1. versionName/short-version from `app.json`.

## Status checks
```bash
RELEASE_CONFIG=.private/release-config.json python3 scripts/store_release.py status   # Google Play
RELEASE_CONFIG=.private/release-config.json python3 scripts/ios_release.py status     # App Store
```

## Gotchas
- **Credentials missing/placeholder** → run `preflight.py`; it names the exact field/file.
- **iOS `MAC verification failed`** importing the p12 → the distribution p12 is newer-encryption; `ios_release.py` auto-handles it, but a manual `build_ios.sh` needs a legacy-re-encrypted p12 (see CREDENTIALS.md).
- **Android `JAVA_HOME`** → `/usr/libexec/java_home` may not find a Homebrew JDK; build scripts fall back to `/opt/homebrew/opt/openjdk@17`. Set `JAVA_HOME` + `ANDROID_HOME` if elsewhere.
- **Google track PUT ReadTimeout** → the upload is idempotent (AAB deduped by sha256); just re-run.
- **Review-gate** (`release/readiness.json`) can block `--submit`; production `aos/auto-upload.sh` uses a plain commit that bypasses it.
- **Moving to a new project**: copy `scripts/`, `aos/`, `ios-upload/`, and this skill; keep an Expo `app.json`/`app.config.js` reading `APP_BUILD_NUMBER`.

## Security
All secrets live in `.private/` (gitignored) — referenced here, never embedded. The
committed `release-config.example.json` holds only placeholders.
