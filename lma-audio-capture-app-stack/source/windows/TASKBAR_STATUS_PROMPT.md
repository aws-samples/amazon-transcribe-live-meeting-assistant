# Prompt: Windows taskbar recording-status (mirror of the macOS Dock feature)

Run this prompt with Claude Code on a Windows machine, from the LMA repo root,
on the `feature/windows-audio-capture-app` branch. Delete this file in the
final commit once the work is done.

---

Pull the latest feature/windows-audio-capture-app branch first (it has new
commits from my Mac). Then implement Windows taskbar recording-status for the
LMA audio capture client in lma-audio-capture-app-stack/source/windows/,
mirroring the macOS Dock feature in commit 4505ebd9
("feat(audio-capture-mac): add Dock presence with recording status").

Context / problem: the app is currently tray-only (no window, no taskbar
button). Tray icons get hidden in the overflow flyout, and when recording
starts there may be no always-visible indicator or stop control. On macOS we
solved this with a Dock icon that shows a red dot + "REC" badge while
recording, plus a right-click menu with Start/Pause/Stop. Build the Windows
equivalent using the taskbar.

Requirements:

1. While recording, the app should have a visible taskbar button whose icon
   shows recording state via TaskbarItemInfo.Overlay (red dot overlay), and
   use ProgressState/ProgressValue paused-yellow when paused if that reads
   well. When idle, revert to the current tray-only behavior (no taskbar
   button) so the app stays unobtrusive between meetings — i.e. the taskbar
   presence appears when recording starts and disappears when it stops.

2. Reuse the existing PanelView as the content of that window (same pattern
   as the existing tray flyout Window in TrayApp.cs — it must be a real
   Window with an HWND so keyboard input works; that lesson is already
   in the code comments). Closing/minimizing the window while recording
   must NOT stop recording — hide to tray instead, but keep the taskbar
   button visible while recording (ShowInTaskbar stays true; consider
   minimizing rather than hiding).

3. Add thumbnail toolbar buttons (TaskbarItemInfo.ThumbButtonInfos) for
   Pause/Resume and Stop so recording can be controlled from the taskbar
   hover preview without opening the window.

4. Add a JumpList (right-click the taskbar button) with Start Recording /
   Pause / Stop / Open Control Panel, matching the macOS Dock right-click
   menu.

5. Do NOT attempt programmatic taskbar pinning — we already removed that in
   commit 60eac1c2 because Windows removed the supported API and probing
   Shell.Application loads noisy third-party shell extensions. Keep manual
   pin instructions in the docs instead.

6. Keep the tray icon behavior exactly as-is (it remains the primary UI).

7. Self-test: run ./build-windows.ps1 -SelfContained and verify the build
   passes its self-test gate. Manually verify: start a recording → taskbar
   button appears with red overlay; pause → overlay/progress changes; stop
   → taskbar button disappears, tray-only again; thumbnail buttons and
   jump list work; closing the window during recording doesn't stop it.

8. Update docs to stay consistent (we keep docs in lockstep with behavior):
   - lma-audio-capture-app-stack/source/windows/README.md
   - docs/audio-capture-app.md (the "Using the system-tray app (Windows)"
     section — describe the recording-time taskbar presence)
   - lma-ai-stack/source/ui/src/components/audio-capture-app-layout/
     AudioCaptureApp.jsx (Windows install/usage sections; run
     npx prettier --write and npx eslint on it; don't introduce new
     lint errors — 3 pre-existing ones are known)
   Reference commit 9b19ebac for the macOS wording pattern.

9. Commit with a conventional-commit message on
   feature/windows-audio-capture-app (delete this prompt file in that
   commit) and push to GitHub.
