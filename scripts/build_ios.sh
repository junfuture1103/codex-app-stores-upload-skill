#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != 'Darwin' ]]; then
  echo 'iOS builds require macOS with Xcode.' >&2
  exit 1
fi
for binary in pnpm xcodebuild python3 security; do
  command -v "$binary" >/dev/null || { echo "Missing command: $binary" >&2; exit 1; }
done
build_mode="${IOS_BUILD_MODE:-device}"
if [[ "$build_mode" != 'device' && "$build_mode" != 'simulator' ]]; then
  echo 'IOS_BUILD_MODE must be device or simulator.' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
native_dir="$repo_root/ios"
output_dir="$repo_root/release-artifacts"
if [[ -L "$native_dir" ]]; then
  echo 'Refusing Expo prebuild --clean: ios is a link.' >&2
  exit 1
fi

manual_signing=0
if [[ "$build_mode" == 'device' ]]; then
  : "${APPLE_TEAM_ID:?Set APPLE_TEAM_ID to the Apple Developer Team ID}"
  if [[ -n "${IOS_P12_PATH:-}" || -n "${IOS_PROFILE_PATH:-}" ]]; then
    manual_signing=1
    : "${IOS_P12_PATH:?Manual signing requires IOS_P12_PATH}"
    : "${IOS_P12_PASSWORD:?Manual signing requires IOS_P12_PASSWORD}"
    : "${IOS_PROFILE_PATH:?Manual signing requires IOS_PROFILE_PATH}"
    [[ -f "$IOS_P12_PATH" ]] || { echo 'IOS_P12_PATH does not exist.' >&2; exit 1; }
    [[ -f "$IOS_PROFILE_PATH" ]] || { echo 'IOS_PROFILE_PATH does not exist.' >&2; exit 1; }
  else
    : "${ASC_KEY_ID:?Automatic signing requires ASC_KEY_ID}"
    : "${ASC_ISSUER_ID:?Automatic signing requires ASC_ISSUER_ID}"
    : "${ASC_KEY_PATH:?Automatic signing requires ASC_KEY_PATH}"
    [[ -f "$ASC_KEY_PATH" ]] || { echo 'ASC_KEY_PATH does not exist.' >&2; exit 1; }
  fi
fi

mkdir -p "$output_dir"
temp_dir="$(mktemp -d)"
keychain_path=''
installed_profile=''
original_keychains=()
cleanup() {
  if [[ -n "$keychain_path" ]]; then
    if ((${#original_keychains[@]})); then
      security list-keychains -d user -s "${original_keychains[@]}" >/dev/null 2>&1 || true
    fi
    security delete-keychain "$keychain_path" >/dev/null 2>&1 || true
  fi
  [[ -z "$installed_profile" ]] || rm -f -- "$installed_profile"
  [[ -z "$temp_dir" || ! -d "$temp_dir" ]] || rm -rf -- "$temp_dir"
}
trap cleanup EXIT

cd "$repo_root"
pnpm install --frozen-lockfile
pnpm typecheck
pnpm exec expo config --json > "$temp_dir/expo-config.json"
bundle_id="$(python3 - "$temp_dir/expo-config.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    config = json.load(source)
bundle = config.get('ios', {}).get('bundleIdentifier')
if not bundle:
    raise SystemExit('Expo ios.bundleIdentifier is required')
print(bundle)
PY
)"
pnpm exec expo prebuild --platform ios --clean
# expo-notifications adds the aps-environment (remote push) entitlement. For a
# local-notifications-only build our App Store profile lacks Push and the archive
# would fail, so strip it — UNLESS APP_PUSH=1, which means remote push is enabled
# and the provisioning profile already includes the Push Notifications capability.
if [[ "${APP_PUSH:-}" != '1' ]]; then
  for ent in "$native_dir"/*/*.entitlements; do
    [[ -f "$ent" ]] && /usr/libexec/PlistBuddy -c 'Delete :aps-environment' "$ent" 2>/dev/null || true
  done
fi
shopt -s nullglob
workspaces=("$native_dir"/*.xcworkspace)
shopt -u nullglob
if ((${#workspaces[@]} != 1)); then
  echo 'Expected exactly one generated xcworkspace.' >&2
  exit 1
fi
workspace="${workspaces[0]}"
xcodebuild -list -json -workspace "$workspace" > "$temp_dir/xcode-list.json"
scheme="$(python3 - "$temp_dir/xcode-list.json" "$(basename "$workspace" .xcworkspace)" "${IOS_SCHEME:-}" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    schemes = json.load(source).get('workspace', {}).get('schemes', [])
preferred, override = sys.argv[2:]
if override:
    if override not in schemes:
        raise SystemExit('IOS_SCHEME is not a shared workspace scheme')
    print(override)
elif preferred in schemes:
    print(preferred)
elif len(schemes) == 1:
    print(schemes[0])
else:
    raise SystemExit('Set IOS_SCHEME to a shared workspace scheme')
PY
)"

if [[ "$build_mode" == 'simulator' ]]; then
  simulator_dir="$(mktemp -d "$output_dir/ios-simulator-XXXXXX")"
  xcodebuild -workspace "$workspace" -scheme "$scheme" -configuration Release \
    -jobs "${APP_BUILD_JOBS:-2}" \
    -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "$simulator_dir/DerivedData" CODE_SIGNING_ALLOWED=YES \
    CODE_SIGN_IDENTITY=- CODE_SIGN_INJECT_BASE_ENTITLEMENTS=YES build
  shopt -s nullglob
  apps=("$simulator_dir/DerivedData/Build/Products/Release-iphonesimulator"/*.app)
  shopt -u nullglob
  if ((${#apps[@]} != 1)); then
    echo 'Expected exactly one built simulator .app.' >&2
    exit 1
  fi
  simulator_zip="$output_dir/app-ios-simulator.zip"
  (cd "$(dirname "${apps[0]}")" && ditto -c -k --sequesterRsrc --keepParent "$(basename "${apps[0]}")" "$simulator_zip")
  (cd "$output_dir" && shasum -a 256 "$(basename "$simulator_zip")" > SHA256SUMS-ios-simulator.txt)
  echo "Ad-hoc signed iOS Simulator build: $simulator_zip"
  exit 0
fi

signing_args=()
signing_certificate=''
profile_name=''
if ((manual_signing)); then
  profile_plist="$temp_dir/profile.plist"
  security cms -D -i "$IOS_PROFILE_PATH" > "$profile_plist"
  profile_uuid="$(/usr/libexec/PlistBuddy -c 'Print :UUID' "$profile_plist")"
  profile_name="$(/usr/libexec/PlistBuddy -c 'Print :Name' "$profile_plist")"
  profile_app_id="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:application-identifier' "$profile_plist")"
  if [[ "$profile_app_id" != "$APPLE_TEAM_ID.$bundle_id" && "$profile_app_id" != "$APPLE_TEAM_ID.*" ]]; then
    echo 'Provisioning profile does not match the team and bundle identifier.' >&2
    exit 1
  fi
  profile_dir="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
  mkdir -p "$profile_dir"
  installed_profile="$profile_dir/$profile_uuid.mobileprovision"
  if [[ -e "$installed_profile" ]]; then
    if ! cmp -s "$IOS_PROFILE_PATH" "$installed_profile"; then
      echo 'A different profile with the same UUID already exists.' >&2
      installed_profile=''
      exit 1
    fi
    # Reuse an identical profile, but do not remove the user's copy on exit.
    installed_profile=''
  else
    cp "$IOS_PROFILE_PATH" "$installed_profile"
  fi

  while IFS= read -r keychain; do
    [[ -z "$keychain" ]] || original_keychains+=("$keychain")
  done < <(security list-keychains -d user | sed -E 's/^[[:space:]]*"(.*)"[[:space:]]*$/\1/')
  keychain_path="$temp_dir/app-release.keychain-db"
  keychain_password="$(openssl rand -hex 24)"
  security create-keychain -p "$keychain_password" "$keychain_path"
  security unlock-keychain -p "$keychain_password" "$keychain_path"
  security set-keychain-settings -lut 21600 "$keychain_path"
  security import "$IOS_P12_PATH" -k "$keychain_path" -P "$IOS_P12_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain_path"
  security list-keychains -d user -s "$keychain_path" "${original_keychains[@]}"
  identity_lines="$(security find-identity -v -p codesigning "$keychain_path" | grep -F "($APPLE_TEAM_ID)" || true)"
  identity_count="$(printf '%s\n' "$identity_lines" | grep -c . || true)"
  if [[ "$identity_count" != '1' ]]; then
    echo 'The supplied P12 must contain one valid signing identity for APPLE_TEAM_ID.' >&2
    exit 1
  fi
  signing_certificate="$(printf '%s\n' "$identity_lines" | awk '{print $2}')"
  if [[ ! "$signing_certificate" =~ ^[[:xdigit:]]{40}$ ]]; then
    echo 'Could not read the distribution identity SHA-1 from the P12.' >&2
    exit 1
  fi
  signing_args=(CODE_SIGN_STYLE=Manual "PROVISIONING_PROFILE_SPECIFIER=$profile_name" "CODE_SIGN_IDENTITY=$signing_certificate")
else
  signing_args=(CODE_SIGN_STYLE=Automatic -allowProvisioningUpdates
    -authenticationKeyPath "$ASC_KEY_PATH" -authenticationKeyID "$ASC_KEY_ID"
    -authenticationKeyIssuerID "$ASC_ISSUER_ID")
fi

run_dir="$(mktemp -d "$output_dir/ios-run-XXXXXX")"
archive_path="$run_dir/the app.xcarchive"
export_dir="$run_dir/export"
mkdir -p "$export_dir"
python3 - "$temp_dir/ExportOptions.plist" "$APPLE_TEAM_ID" "$bundle_id" "$profile_name" "$manual_signing" "$signing_certificate" <<'PY'
import plistlib, sys
path, team, bundle, profile, manual, certificate = sys.argv[1:]
options = {
    'method': 'app-store-connect',
    'destination': 'export',
    'signingStyle': 'manual' if manual == '1' else 'automatic',
    'teamID': team,
    'manageAppVersionAndBuildNumber': False,
}
if manual == '1':
    options['signingCertificate'] = certificate
    options['provisioningProfiles'] = {bundle: profile}
with open(path, 'wb') as output:
    plistlib.dump(options, output)
PY

xcodebuild -workspace "$workspace" -scheme "$scheme" -configuration Release \
  -jobs "${APP_BUILD_JOBS:-2}" \
  -destination 'generic/platform=iOS' -archivePath "$archive_path" \
  "DEVELOPMENT_TEAM=$APPLE_TEAM_ID" "${signing_args[@]}" archive
export_auth=()
if ((!manual_signing)); then
  export_auth=(-allowProvisioningUpdates -authenticationKeyPath "$ASC_KEY_PATH"
    -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID")
fi
# ${arr[@]+...} keeps bash 3.2 'set -u' from failing on an empty array.
xcodebuild -exportArchive -archivePath "$archive_path" -exportPath "$export_dir" \
  -exportOptionsPlist "$temp_dir/ExportOptions.plist" ${export_auth[@]+"${export_auth[@]}"}
shopt -s nullglob
ipas=("$export_dir"/*.ipa)
shopt -u nullglob
if ((${#ipas[@]} != 1)); then
  echo 'Expected exactly one exported IPA.' >&2
  exit 1
fi
ipa_dest="$output_dir/app-ios-release.ipa"
cp "${ipas[0]}" "$ipa_dest"
(cd "$output_dir" && shasum -a 256 "$(basename "$ipa_dest")" > SHA256SUMS-ios.txt)
echo "iOS release build: $ipa_dest"
echo "iOS Xcode archive: $archive_path"
