# 크리덴셜 발급 가이드

이 한 파일(`release-config.json`)만 채우면 이 스킬이 iOS·Android 둘 다 올립니다.
`release-config.example.json` 을 복사해 **`.private/release-config.json`** 에 저장하세요
(`.private/` 는 .gitignore 됨 — 비밀키가 git에 올라가지 않습니다). 파일 경로 필드는
**절대경로** 또는 config 파일 기준 상대경로 모두 됩니다.

```bash
mkdir -p .private
cp .claude/skills/store-upload/release-config.example.json .private/release-config.json
# 에디터로 .private/release-config.json 의 REQUIRED 값들을 채운다
python3 .claude/skills/store-upload/preflight.py   # 다 채웠는지 자동 검사
```

---

## `bundleId`
앱 번들 ID. `app.json` 의 `expo.ios.bundleIdentifier` / `expo.android.package` 와 **동일**해야 함.
예: `com.yourcompany.yourapp`. (preflight가 일치 여부를 검사함.)

---

## 🍎 apple — App Store Connect

| 필드 | 무엇 | 어디서 |
|---|---|---|
| `teamId` | Apple Developer 팀 ID (10자) | [developer.apple.com](https://developer.apple.com/account) → Membership details → Team ID |
| `keyId` | App Store Connect **API 키** ID | [ASC → Users and Access → Integrations → App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api) → 키 생성(Admin 또는 App Manager) → Key ID |
| `issuerId` | 같은 API 화면 상단의 Issuer ID (UUID) | 위와 같은 화면 |
| `keyPath` | 그 API 키의 **`.p8` 파일** | 키 생성 시 **1회만** 다운로드 가능 (`AuthKey_XXXX.p8`) → 안전한 곳에 두고 절대경로 지정 |
| `appId` | 앱의 **숫자 ID** | ASC에서 앱 선택 → App Information, 또는 스토어 URL `apps.apple.com/.../id<이 숫자>`. (심사 제출 시 필요) |
| `p12Path` | **배포 인증서**(Apple Distribution) `.p12` | Keychain Access에서 "Apple Distribution" 인증서+개인키를 .p12로 내보내기, 또는 developer.apple.com → Certificates |
| `p12Password` | 그 .p12 내보낼 때 설정한 비밀번호 | 본인이 정함 |
| `profilePath` | **App Store 프로비저닝 프로파일** `.mobileprovision` | developer.apple.com → Profiles → App Store 배포용 생성 → 다운로드. (bundleId와 배포 인증서가 묶여야 함) |

> ⚠️ 빌드가 `security import` 에서 `MAC verification failed` 로 실패하면 .p12가 최신
> 암호화라 macOS가 못 읽는 경우입니다. `ios_release.py release` 는 자동으로 레거시
> 형식으로 재암호화하지만, 수동 빌드 시엔 레거시 .p12가 필요할 수 있습니다:
> `openssl pkcs12 -in dist.p12 -nodes -legacy -passin pass:PW | openssl pkcs12 -export -out dist-legacy.p12 -passout pass:PW`

---

## 🤖 google — Google Play

| 필드 | 무엇 | 어디서 |
|---|---|---|
| `serviceAccountPath` | Play Console **서비스 계정 JSON 키** | [Google Cloud Console](https://console.cloud.google.com) → IAM → 서비스 계정 생성 → 키(JSON) 다운로드. 그 뒤 [Play Console → Users and permissions](https://play.google.com/console) 에서 그 서비스계정을 초대하고 **릴리스 관리 + 앱 정보** 권한 부여 |
| `developerId` | Play Console 개발자 계정 ID (숫자) | Play Console URL `play.google.com/console/u/0/developers/<이 숫자>` |

> 서명 키(아래 android)는 **업로드 키**입니다. 앱이 Google Play 앱 서명(Play App Signing)을
> 쓰면 업로드 키로 서명한 AAB를 Google이 받아 재서명합니다. 첫 업로드 때 등록한 업로드 키와
> **같은 키**여야 합니다(SHA1 불일치 시 거부).

---

## 🔑 android — 업로드 서명 키

| 필드 | 무엇 | 어디서 |
|---|---|---|
| `keystorePath` | 업로드 **keystore** (`.jks`/`.keystore`) | 최초 1회 생성: `keytool -genkeypair -v -keystore upload.jks -alias upload -keyalg RSA -keysize 2048 -validity 10000`. **분실 금지** — Play에 등록된 키와 같아야 함 |
| `storePassword` | keystore 비밀번호 | 생성 시 본인이 정함 |
| `keyAlias` | 키 별칭 | 생성 시 지정(`-alias`), 보통 `upload` |
| `keyPassword` | 키 비밀번호 | 생성 시 지정(보통 storePassword와 동일) |

---

## 보안
- 실제 값은 **`.private/release-config.json`** 에만 (gitignore됨). 이 example/가이드에는 비밀값 없음.
- `.p8`/`.p12`/`.jks`/서비스계정 JSON 원본도 git에 올리지 말 것(`.private/` 등 gitignore 경로에).
- 리포가 public이면 `.private/` 가 정말 ignore되는지(`git check-ignore .private/release-config.json`) 꼭 확인.
