<p align="center">
  <img src="assets/logo.svg" alt="Store Upload Skill" width="620">
</p>

<h1 align="center">App Stores Upload Skill</h1>

<p align="center">
  <b>Ship an Expo / React Native app to the <i>Apple App Store</i> and <i>Google Play</i> from one credential file.</b><br>
  Fill in your signing keys once → build, sign, upload, and submit with a single command.
</p>

<p align="center">
  <img alt="platform" src="https://img.shields.io/badge/platform-iOS%20%7C%20Android-7C5CFF">
  <img alt="expo" src="https://img.shields.io/badge/stack-Expo%20%2F%20React%20Native-22D3EE">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-brightgreen">
  <img alt="secrets" src="https://img.shields.io/badge/your%20keys-stay%20on%20your%20PC-red">
</p>

---

This is a drop-in **coding-agent skill + release toolkit**. You bring the credentials;
it handles versionCode/build-number bumping, code signing, p12 quirks, uploading via
`altool` / Google Play API, and (optionally) submitting for review. No app IDs are
hard-coded — every store identifier is read from **your** config and `app.json`.

> ⛔ **Your credentials never live in this repo.** You download this toolkit and put
> your own keys on your own machine, in a git-ignored folder. Nothing secret is here.

---

## ✅ The credentials you need (the complete list)

Put them all in **one file** (`.private/release-config.json`). That's the only thing you fill in.

### 🍎 Apple — App Store Connect
| # | Credential | Config field | Where to get it |
|---|---|---|---|
| 1 | **Team ID** | `apple.teamId` | developer.apple.com → Membership → Team ID (10 chars) |
| 2 | **ASC API Key ID** | `apple.keyId` | App Store Connect → Users and Access → Integrations → **App Store Connect API** → create key |
| 3 | **ASC API Issuer ID** | `apple.issuerId` | same screen (UUID at the top) |
| 4 | **ASC API Key file** `.p8` | `apple.keyPath` | downloaded **once** when you create the key (`AuthKey_XXXX.p8`) |
| 5 | **App ID** (numeric) | `apple.appId` | ASC → your app, or the store URL `…/id<this>` *(needed only to submit)* |
| 6 | **Distribution certificate** `.p12` + password | `apple.p12Path`, `apple.p12Password` | Keychain Access → export "Apple Distribution" cert+key |
| 7 | **App Store provisioning profile** `.mobileprovision` | `apple.profilePath` | developer.apple.com → Profiles → App Store distribution |

### 🤖 Google — Google Play
| # | Credential | Config field | Where to get it |
|---|---|---|---|
| 8 | **Service account JSON key** | `google.serviceAccountPath` | Google Cloud → service account → JSON key, then invite it in Play Console with **Release** permission |
| 9 | **Developer ID** | `google.developerId` | Play Console URL `…/developers/<this>` |

### 🔑 Android — upload signing key
| # | Credential | Config field | Where to get it |
|---|---|---|---|
| 10 | **Upload keystore** `.jks` + passwords + alias | `android.keystorePath`, `storePassword`, `keyAlias`, `keyPassword` | `keytool -genkeypair -keystore upload.jks -alias upload -keyalg RSA -keysize 2048 -validity 10000` (keep it forever — must match Play) |

Plus one shared value: **`bundleId`** (must equal `app.json`'s `expo.ios.bundleIdentifier` / `expo.android.package`).

📖 Full step-by-step for every item → **[CREDENTIALS.md](.claude/skills/store-upload/CREDENTIALS.md)**

---

## 🚀 Quickstart

```bash
# 1) Copy these folders into your Expo project root:
#    .claude/skills/store-upload/   scripts/   aos/   ios-upload/   plugins/   release/

# 2) Create your private credential file (git-ignored) and fill in the 10 items above
mkdir -p .private
cp .claude/skills/store-upload/release-config.example.json .private/release-config.json
$EDITOR .private/release-config.json

# 3) Verify everything is set up — these two checkers tell you exactly what's missing
python3 .claude/skills/store-upload/preflight.py          # fields present + toolchain ready
python3 .claude/skills/store-upload/check-credentials.py  # the key files are valid & unexpired
```

Then ship:

```bash
# Android → Google Play
bash aos/auto-upload.sh                 # production, versionCode = Play max + 1
bash aos/auto-upload.sh --track internal
bash aos/auto-upload.sh --dry-run

# iOS → App Store Connect (macOS + Xcode)
bash ios-upload/auto-upload.sh          # build + upload to TestFlight
bash ios-upload/auto-upload.sh --submit # + submit for App Review
```

---

## 🔎 Two checkers (so you never guess)

| Checker | What it proves |
|---|---|
| **`preflight.py`** | Every required field is filled (no `REQUIRED` left), every referenced file exists, `bundleId` matches `app.json`, and the toolchain (node · pnpm · JDK 17 · Xcode · Android SDK) is ready. |
| **`check-credentials.py`** | Opens the actual secret files and validates them: the `.p8` is a real API key, the `.p12` opens with your password and is an **unexpired distribution cert**, the `.mobileprovision` matches your team + bundle and isn't expired, the service-account JSON is well-formed, and the keystore contains your alias (prints the upload-key fingerprint). |

Both are read-only, print no secrets, and exit non-zero if something's wrong — perfect for CI or a pre-release gate.

---

## 📋 Requirements

- An **Expo** project whose `app.config.js` reads `APP_BUILD_NUMBER` into `ios.buildNumber` / `android.versionCode` (snippet: [`app.config.example.js`](app.config.example.js)), and includes the bundled Android signing plugin.
- **Node + pnpm**, **Python 3** with `cryptography` + `requests` (`pip install -r release/requirements.txt`).
- **Android**: JDK 17 + Android SDK. **iOS**: macOS + Xcode.

The toolkit auto-handles the usual pain points: versionCode/build-number bumping, the
macOS **`MAC verification failed`** p12 re-encryption, `JAVA_HOME` discovery, and Google
Play upload `ReadTimeout` retries (uploads are idempotent).

---

## 🔒 Security

- Every secret lives under **`.private/`** (git-ignored by the included [`.gitignore`](.gitignore)). This repo ships **only placeholders**.
- Never commit `.p8` / `.p12` / `.jks` / service-account JSON / `.mobileprovision`.
- If your fork is public, confirm the ignore works: `git check-ignore .private/release-config.json`.

---

## 📂 What's inside

```
.claude/skills/store-upload/   # the agent skill: SKILL.md, CREDENTIALS.md, template, preflight.py, check-credentials.py
scripts/                       # build + upload engine (gradle/xcodebuild, App Store Connect API, Google Play API)
aos/                           # Android one-command runner + next-versionCode
ios-upload/                    # iOS one-command runner
plugins/                       # Expo config plugins: Android release signing, iOS aps-entitlement strip
release/requirements.txt       # Python deps
app.config.example.js          # how to wire APP_BUILD_NUMBER + the signing plugin
```

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship your app.
