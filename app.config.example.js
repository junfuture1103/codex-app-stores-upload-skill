// Wire APP_BUILD_NUMBER into both stores + add the Android release-signing plugin.
// Merge this into your project's app.config.js (or app.config.ts).
const base = require('./app.json').expo;

module.exports = () => {
  const raw = process.env.APP_BUILD_NUMBER;            // set by the upload scripts
  if (raw && !/^\d+$/.test(raw)) throw new Error('APP_BUILD_NUMBER must be an integer');

  // Remote push is opt-in: set APP_PUSH=1 (or leave default) to keep FCM/APNs wired.
  const push = process.env.APP_PUSH !== '0';
  let plugins = base.plugins || [];
  // The signing plugin must be ACTIVE for `gradlew bundleRelease` to use your keystore:
  //   './plugins/withReleaseSigning.cjs'
  // The aps-entitlement strip keeps non-push archives valid; drop it when push is on:
  if (push) plugins = plugins.filter((p) => p !== './plugins/withNoApsEntitlement.cjs');

  return {
    ...base,
    plugins,
    ios: { ...base.ios, buildNumber: raw || base.ios?.buildNumber },
    android: { ...base.android, versionCode: raw ? Number(raw) : base.android?.versionCode },
  };
};
