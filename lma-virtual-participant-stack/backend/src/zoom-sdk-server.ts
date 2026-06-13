import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';
import { createRequire } from 'module';
import { AddressInfo } from 'net';

const require = createRequire(import.meta.url);

function resolveSdk(): { distDir: string; version: string } {
    const pkgJsonPath = require.resolve('@zoom/meetingsdk/package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgJsonPath, 'utf-8')) as { version: string };
    return { distDir: path.join(path.dirname(pkgJsonPath), 'dist'), version: pkg.version };
}

const CONTENT_TYPES: Record<string, string> = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.wasm': 'application/wasm',
    '.map': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
};

function contentTypeFor(filePath: string): string {
    return CONTENT_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
}

function setIsolationHeaders(res: http.ServerResponse): void {
    res.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
    res.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
    res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
}

export interface ZoomSdkServerHandle {
    origin: string;
    version: string;
    close: () => Promise<void>;
}

export function buildZoomSdkPageHtml(version: string): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LMA Zoom</title>
  <style>html,body,#zmmtg-root{width:100%;height:100%;margin:0;background:#0b0b0b;}</style>
  <script src="/lib/vendor/react.min.js"></script>
  <script src="/lib/vendor/react-dom.min.js"></script>
  <script src="/lib/vendor/redux.min.js"></script>
  <script src="/lib/vendor/redux-thunk.min.js"></script>
  <script src="/lib/vendor/lodash.min.js"></script>
  <script src="/zoom-meeting-${version}.min.js"></script>
</head>
<body>
  <div id="zmmtg-root"></div>
  <script>
    (function () {
      var w = window;
      w.__lmaSdkReady = false;
      w.__lmaMeetingStatus = 0;
      w.__lmaJoinError = null;
      w.__lmaInWaitingRoom = false;
      w.__lmaParticipantCount = 1;
      w.__lmaActiveSpeaker = '';
      w.__lmaSelfLeaveReason = null;
      w.__lmaChatQueue = [];

      function log(m) { try { console.log('[LMA-ZoomSDK] ' + m); } catch (e) {} }

      function setupListeners(ZoomMtg) {
        ZoomMtg.inMeetingServiceListener('onMeetingStatus', function (data) {
          if (data && typeof data.meetingStatus !== 'undefined') w.__lmaMeetingStatus = data.meetingStatus;
          else if (data && typeof data.status !== 'undefined') w.__lmaMeetingStatus = data.status;
          log('meetingStatus=' + w.__lmaMeetingStatus);
        });
        ZoomMtg.inMeetingServiceListener('onUserIsInWaitingRoom', function () {
          w.__lmaInWaitingRoom = true;
          log('in waiting room');
        });
        ZoomMtg.inMeetingServiceListener('onUserJoin', function (data) {
          var n = Array.isArray(data) ? data.length : 1;
          w.__lmaParticipantCount += n;
        });
        ZoomMtg.inMeetingServiceListener('onUserLeave', function (data) {
          if (data && typeof data.reasonCode !== 'undefined') w.__lmaSelfLeaveReason = data.reasonCode;
          var n = Array.isArray(data) ? data.length : 1;
          w.__lmaParticipantCount = Math.max(1, w.__lmaParticipantCount - n);
        });
        ZoomMtg.inMeetingServiceListener('onActiveSpeaker', function (data) {
          var first = Array.isArray(data) ? data[0] : data;
          if (first && first.userName) w.__lmaActiveSpeaker = first.userName;
        });
        ZoomMtg.inMeetingServiceListener('onReceiveChatMsg', function (data) {
          try {
            var senderName = (data && (data.senderName || (data.sender && data.sender.name))) || '';
            var text = (data && (data.message || data.content || data.text)) || '';
            var selfId = (data && (data.senderId || (data.sender && data.sender.userId)));
            // Skip our own messages echoed back, so intro/start/exit text we
            // posted is never re-parsed as a participant command.
            if (selfId && w.__lmaSelfUserId && selfId === w.__lmaSelfUserId) return;
            w.__lmaChatQueue.push({ senderName: senderName, text: String(text) });
          } catch (e) { log('chat parse error: ' + e); }
        });
      }

      w.__lmaZoomJoin = function () {
        var ZoomMtg = w.ZoomMtg;
        var cfg = w.__lmaZoomConfig || {};
        if (!ZoomMtg) { w.__lmaJoinError = { errorCode: -1, reason: 'ZoomMtg not loaded' }; return; }
        ZoomMtg.init({
          leaveUrl: cfg.leaveUrl || (w.location.origin + '/left.html'),
          patchJsMedia: true,
          disableInvite: true,
          isShowJoiningErrorDialog: true,
          success: function () {
            ZoomMtg.join({
              meetingNumber: cfg.meetingNumber,
              userName: cfg.userName || 'LMA',
              passWord: cfg.passWord || '',
              signature: cfg.signature,
              success: function () {
                log('join success');
                try {
                  ZoomMtg.getCurrentUser({
                    success: function (d) {
                      var u = d && d.result && d.result.currentUser;
                      if (u && u.userId) w.__lmaSelfUserId = u.userId;
                    },
                  });
                } catch (e) { /* best-effort */ }
              },
              error: function (err) {
                log('join error: ' + JSON.stringify(err));
                w.__lmaJoinError = { errorCode: (err && err.errorCode), reason: (err && (err.reason || err.result)) || 'join failed' };
              },
            });
          },
          error: function (err) {
            log('init error: ' + JSON.stringify(err));
            w.__lmaJoinError = { errorCode: (err && err.errorCode), reason: (err && (err.reason || err.result)) || 'init failed' };
          },
        });
      };

      w.__lmaPreviewActive = function () {
        return !!document.querySelector('.preview-join-button, button.preview-join-button');
      };

      // Authoritative participant count from the in-meeting toolbar counter
      // (same source the DOM handler trusts). Returns -1 when the counter
      // isn't present so callers can fall back to the event-based count and
      // never falsely conclude "alone".
      w.__lmaParticipantCountDom = function () {
        var el = document.querySelector('.footer-button__number-counter')
          || document.querySelector('[class*="number-counter"]');
        if (!el) return -1;
        var n = parseInt((el.textContent || '').replace(/[^0-9]/g, ''), 10);
        return Number.isFinite(n) ? n : -1;
      };

      w.__lmaStartPopupDismiss = function () {
        if (w.__lmaPopupDismissStarted) return;
        w.__lmaPopupDismissStarted = true;
        var TEXT_PATTERNS = ['recording', 'consent', 'recorded', 'language interpretation',
          'translation', 'request language', 'by joining', 'acknowledg', 'ai companion',
          'meeting summary', 'smart summary', 'ai-generated', 'generated by ai', 'transcript'];
        var MODAL_SELECTORS = ['.zm-modal', '.zm-modal-legacy', '[role="alertdialog"]',
          '.ReactModal__Content', '.recording-disclaimer-dialog'];
        var ACTION_BTN_SELECTORS = ['.zm-modal-footer-default-actions button.zm-btn--primary',
          'button.zm-btn--primary', 'button.zm-btn-legacy.zm-btn--primary', 'button.zm-btn__outline--blue'];
        var TEXT_BTNS = ['got it', 'i agree', 'ok', 'okay', 'continue', 'accept', 'consent', 'agree', 'close', 'dismiss'];
        function dismiss() {
          for (var i = 0; i < MODAL_SELECTORS.length; i++) {
            var modals = document.querySelectorAll(MODAL_SELECTORS[i]);
            for (var j = 0; j < modals.length; j++) {
              var m = modals[j];
              if (!m || m.offsetParent === null) continue;
              var t = (m.textContent || '').toLowerCase();
              var relevant = false;
              for (var k = 0; k < TEXT_PATTERNS.length; k++) { if (t.indexOf(TEXT_PATTERNS[k]) !== -1) { relevant = true; break; } }
              if (!relevant) continue;
              var clicked = false;
              for (var a = 0; a < ACTION_BTN_SELECTORS.length; a++) {
                var btn = m.querySelector(ACTION_BTN_SELECTORS[a]);
                if (btn && btn.offsetParent !== null) { log('dismissing popup: "' + t.substring(0, 60).trim() + '" via "' + (btn.textContent || '').trim() + '"'); btn.click(); clicked = true; break; }
              }
              if (clicked) continue;
              var all = m.querySelectorAll('button');
              for (var b = 0; b < all.length; b++) {
                var bt = (all[b].textContent || '').trim().toLowerCase();
                if (TEXT_BTNS.indexOf(bt) !== -1) { log('dismissing popup by text: "' + bt + '"'); all[b].click(); break; }
              }
            }
          }
        }
        dismiss();
        var obs = new MutationObserver(function () { dismiss(); });
        obs.observe(document.body, { childList: true, subtree: true });
        setInterval(dismiss, 1000);
      };

      w.__lmaMediaCandidatesLogged = false;
      w.__lmaMediaState = function () {
        var btns = Array.prototype.slice.call(
          document.querySelectorAll('button[aria-label],[role="button"][aria-label]')
        );
        var cands = btns.map(function (b) {
          return {
            label: (b.getAttribute('aria-label') || '').toLowerCase(),
            svg: (b.querySelector('svg') && b.querySelector('svg').getAttribute('class') || '').toLowerCase(),
          };
        }).filter(function (c) {
          return /audio|mute|video|camera/.test(c.label) || /audio|video/.test(c.svg);
        });
        if (!w.__lmaMediaCandidatesLogged && cands.length) {
          w.__lmaMediaCandidatesLogged = true;
          log('media button candidates: ' + JSON.stringify(cands.slice(0, 10)));
        }
        var labels = cands.map(function (c) { return c.label; });
        var svgs = cands.map(function (c) { return c.svg; });
        var hasLabel = function (re) { return labels.some(function (l) { return re.test(l); }); };
        var hasSvg = function (re) { return svgs.some(function (s) { return re.test(s); }); };

        var needJoinAudio = hasLabel(/join audio|connect audio|computer audio/);
        var audioJoined = hasLabel(/^mute\\b|^unmute\\b|mute my|unmute my/) || hasSvg(/svgaudio/);
        var muted = hasLabel(/^unmute\\b|unmute my/) || hasSvg(/svgaudiounmute/);
        var videoOff = hasLabel(/start video|start my video/) || hasSvg(/svgvideooff|video-off/);
        var videoOn = hasLabel(/stop video|stop my video/) || hasSvg(/svgvideoon/);
        return {
          needJoinAudio: needJoinAudio,
          audioJoined: audioJoined,
          muted: muted,
          videoOff: videoOff && !videoOn,
        };
      };

      w.__lmaSendChat = function (message) {
        try { if (w.ZoomMtg) w.ZoomMtg.sendChat({ message: message }); } catch (e) { log('sendChat error: ' + e); }
      };

      w.__lmaLeave = function () {
        try { if (w.ZoomMtg) w.ZoomMtg.leaveMeeting({}); } catch (e) { log('leave error: ' + e); }
      };

      function init() {
        var ZoomMtg = w.ZoomMtg;
        if (!ZoomMtg) { log('ZoomMtg global missing — SDK script failed to load'); return; }
        ZoomMtg.setZoomJSLib(w.location.origin + '/lib', '/av');
        ZoomMtg.preLoadWasm();
        ZoomMtg.prepareWebSDK();
        setupListeners(ZoomMtg);
        w.__lmaSdkReady = true;
        log('SDK prepared, crossOriginIsolated=' + w.crossOriginIsolated);
      }

      if (document.readyState === 'complete' || document.readyState === 'interactive') init();
      else document.addEventListener('DOMContentLoaded', init);
    })();
  </script>
</body>
</html>`;
}

export async function startZoomSdkServer(): Promise<ZoomSdkServerHandle> {
    const { distDir, version } = resolveSdk();
    const pageHtml = buildZoomSdkPageHtml(version);
    const leftHtml = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Left</title></head><body></body></html>';

    const server = http.createServer((req, res) => {
        setIsolationHeaders(res);
        const rawPath = (req.url || '/').split('?')[0];
        const urlPath = decodeURIComponent(rawPath);

        if (urlPath === '/' || urlPath === '/index.html') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(pageHtml);
            return;
        }
        if (urlPath === '/left.html') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(leftHtml);
            return;
        }

        const candidate = path.normalize(path.join(distDir, urlPath));
        if (candidate !== distDir && !candidate.startsWith(distDir + path.sep)) {
            res.writeHead(403);
            res.end('forbidden');
            return;
        }
        fs.readFile(candidate, (err, data) => {
            if (err) {
                res.writeHead(404);
                res.end('not found');
                return;
            }
            res.writeHead(200, { 'Content-Type': contentTypeFor(candidate) });
            res.end(data);
        });
    });

    await new Promise<void>((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', () => resolve());
    });

    const port = (server.address() as AddressInfo).port;
    const origin = `http://127.0.0.1:${port}`;
    console.log(`[zoom-sdk] embed server listening on ${origin} (SDK v${version})`);

    return {
        origin,
        version,
        close: () =>
            new Promise<void>((resolve) => {
                server.close(() => resolve());
            }),
    };
}
