# noVNC Version Compatibility Note

## Issue
The noVNC library version 1.6.0 introduced a top-level `await` statement in `lib/util/browser.js` that is incompatible with CommonJS bundlers like webpack/babel used by react-scripts.

## Error
```
Module parse failed: Top-level-await is only supported in EcmaScript Modules
```

## Root Cause
The problematic line in noVNC 1.6.0's `lib/util/browser.js`:
```javascript
exports.supportsWebCodecsH264Decode = supportsWebCodecsH264Decode = await _checkWebCodecsH264DecodeSupport();
```

This top-level await was added to check for H.264 WebCodecs support, but it breaks bundling with tools that convert ES modules to CommonJS.

## Solution
**Downgrade to noVNC 1.5.0** which does not have this issue.

The package.json has been updated to pin the version:
```json
"@novnc/novnc": "1.5.0"
```

## References
- GitHub Issue: https://github.com/novnc/noVNC/issues/1943
- Multiple projects affected: cockpit-machines, jupyter-remote-desktop-proxy, and others
- The noVNC team is working on ES module support for npm, but until then, version 1.5.0 is the stable choice for bundling

## Future Considerations
When noVNC releases a version with proper ES module support for npm (likely 1.7.0+), we can upgrade. Monitor:
- https://github.com/novnc/noVNC/issues/1943
- https://github.com/novnc/noVNC/pull/1944

## Impact
Version 1.5.0 lacks the H.264 WebCodecs detection feature, but this does not affect core VNC functionality. All standard VNC encodings (Raw, CopyRect, RRE, Hextile, ZRLE, Tight, etc.) work correctly.
