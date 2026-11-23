const { withAppBuildGradle } = require('expo/config-plugins');

const MARKER = '// APP_RELEASE_SIGNING';

function findBlock(source, name, from = 0, until = source.length) {
  const matcher = new RegExp(`\\b${name}\\s*\\{`, 'g');
  matcher.lastIndex = from;
  let match;
  while ((match = matcher.exec(source)) && match.index < until) {
    const open = source.indexOf('{', match.index);
    let depth = 1;
    let quote = null;
    let lineComment = false;
    let blockComment = false;
    for (let i = open + 1; i < source.length; i += 1) {
      const char = source[i];
      const next = source[i + 1];
      if (lineComment) {
        if (char === '\n') lineComment = false;
        continue;
      }
      if (blockComment) {
        if (char === '*' && next === '/') { blockComment = false; i += 1; }
        continue;
      }
      if (quote) {
        if (char === '\\') { i += 1; continue; }
        if (char === quote) quote = null;
        continue;
      }
      if (char === '/' && next === '/') { lineComment = true; i += 1; continue; }
      if (char === '/' && next === '*') { blockComment = true; i += 1; continue; }
      if (char === '\'' || char === '"') { quote = char; continue; }
      if (char === '{') depth += 1;
      if (char === '}') depth -= 1;
      if (depth === 0) return { start: match.index, open, close: i };
    }
    throw new Error(`Unclosed ${name} block in android/app/build.gradle`);
  }
  return null;
}

function patchGradle(source) {
  if (source.includes(MARKER)) return source;
  const android = findBlock(source, 'android');
  if (!android) throw new Error('Could not find android block in android/app/build.gradle');
  const signingConfigs = findBlock(source, 'signingConfigs', android.open + 1, android.close);
  const buildTypes = findBlock(source, 'buildTypes', android.open + 1, android.close);
  if (!signingConfigs || !buildTypes) {
    throw new Error('Could not find signingConfigs/buildTypes in android/app/build.gradle');
  }
  const releaseBuild = findBlock(source, 'release', buildTypes.open + 1, buildTypes.close);
  if (!releaseBuild) throw new Error('Could not find release build type in android/app/build.gradle');
  if (findBlock(source, 'release', signingConfigs.open + 1, signingConfigs.close)) {
    throw new Error('A release signingConfig already exists; update this signing plugin before building');
  }

  const releaseSigning = `
        ${MARKER}
        release {
            def appValue = { key ->
                def envValue = System.getenv(key)
                return envValue ? envValue : project.findProperty(key)
            }
            def appStorePath = appValue('APP_ANDROID_KEYSTORE_PATH')
            def appStorePassword = appValue('APP_ANDROID_STORE_PASSWORD')
            def appKeyAlias = appValue('APP_ANDROID_KEY_ALIAS')
            def appKeyPassword = appValue('APP_ANDROID_KEY_PASSWORD')
            def appRequireSigning = appValue('APP_REQUIRE_RELEASE_SIGNING') == '1'
            if (appRequireSigning && (!appStorePath || !appStorePassword || !appKeyAlias || !appKeyPassword)) {
                throw new GradleException('the app release signing variables are missing')
            }
            if (appStorePath && appStorePassword && appKeyAlias && appKeyPassword) {
                storeFile file(appStorePath.toString())
                storePassword appStorePassword.toString()
                keyAlias appKeyAlias.toString()
                keyPassword appKeyPassword.toString()
            }
        }
`;

  let updated = source.slice(0, signingConfigs.open + 1) + releaseSigning + source.slice(signingConfigs.open + 1);
  const updatedBuildTypes = findBlock(updated, 'buildTypes', findBlock(updated, 'android').open + 1);
  const updatedRelease = findBlock(updated, 'release', updatedBuildTypes.open + 1, updatedBuildTypes.close);
  const releaseBody = updated.slice(updatedRelease.open + 1, updatedRelease.close);
  // The RN/Expo template may write either `signingConfig signingConfigs.debug`
  // or `signingConfig = signingConfigs.debug` (newer templates use `=`). Match
  // both so we REPLACE the debug assignment rather than leaving it to win.
  const debugSigning = /\bsigningConfig\s*=?\s*signingConfigs\.debug\b/;
  const anySigning = /\bsigningConfig\s*=?\s*signingConfigs\.\w+\b/;
  let newReleaseBody;
  if (debugSigning.test(releaseBody)) {
    newReleaseBody = releaseBody.replace(debugSigning, 'signingConfig signingConfigs.release');
  } else if (anySigning.test(releaseBody)) {
    newReleaseBody = releaseBody.replace(anySigning, 'signingConfig signingConfigs.release');
  } else {
    newReleaseBody = '\n            signingConfig signingConfigs.release' + releaseBody;
  }
  updated = updated.slice(0, updatedRelease.open + 1) + newReleaseBody + updated.slice(updatedRelease.close);
  return updated;
}

module.exports = function withReleaseSigning(config) {
  return withAppBuildGradle(config, (next) => {
    if (next.modResults.language !== 'groovy') {
      throw new Error('the app release signing plugin expects Groovy android/app/build.gradle');
    }
    next.modResults.contents = patchGradle(next.modResults.contents);
    return next;
  });
};

module.exports.patchGradle = patchGradle;
