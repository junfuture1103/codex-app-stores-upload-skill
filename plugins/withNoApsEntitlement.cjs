// expo-notifications adds the `aps-environment` entitlement (for REMOTE push).
// the app only uses LOCAL notifications (scheduled + foreground), which do not
// require that entitlement — and our App Store provisioning profile does not
// include the Push Notifications capability. Strip it so the archive succeeds.
// (To enable remote push later: enable Push on the App ID, regenerate the
// provisioning profile, add an APNs key, and remove this plugin.)
const { withEntitlementsPlist } = require('expo/config-plugins');

module.exports = function withNoApsEntitlement(config) {
  return withEntitlementsPlist(config, (cfg) => {
    delete cfg.modResults['aps-environment'];
    return cfg;
  });
};
