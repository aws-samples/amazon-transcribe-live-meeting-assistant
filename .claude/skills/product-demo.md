# Skill: Record a product demo video — Live Meeting Assistant (LMA)

Use this when the user wants a **video that shows a feature to other people** —
"make a demo of the unreleased changes", "record a product demo of PR #912",
"demo meeting consent for the team", "turn the 0.4.0 changelog into a
walkthrough".

The demo is driven by you, through the chrome-devtools MCP server, in a debug
Chrome signed in to a deployed stack. A sidecar recorder (`scripts/ux_recorder.py`)
attaches to the same tab, captures what it shows, and afterwards renders a
narrated, captioned mp4 with a title card, one chapter per step and a **Key
takeaways** end card. Narration is Amazon Polly reading lines you write; mistakes
are re-taken, never kept.

**The run is collaborative by design.** You propose three demos, the user picks
one, you write the storyboard, the user confirms it, and only then does a browser
get driven. Do not skip the proposal step because the request looked specific:
"demo PR #912" still has three defensible cuts (the headline change, the whole PR,
the user story it serves), and the user is the one who knows who will watch it.

**Drive the browser and record what it shows.** Never narrate a screen you did not
load, and never describe a capability the stack does not have deployed.

---

## The shape of a run

1. **Gather the material** — changelog entries, a PR, or a named feature.
2. **Check the stack has it** — the version and build date in the **Deployment
   Info** section of the side navigation; a change on an unmerged branch is not
   demoable on a stack that does not run it.
3. **Propose three storyboards** and let the user pick one (`AskUserQuestion`).
4. **Write the chosen storyboard in full** into `scripts/demo_storyboards.yaml`
   and confirm it once ("record as written" / "change something").
5. **Prepare fixtures and rehearse unrecorded.** A meeting takes minutes to
   transcribe and summarize; the demo should open on a finished state, and every
   click should already have landed once before the recorder is running.
6. **Record**, one `mark --say` per chapter.
7. **Render, watch the pacing and click tables, re-take if needed.**
8. **Hand over**: the mp4, the docs entry draft, and the saved storyboard.

Only steps 3 and 4 block on the user. Everything else runs to completion.

---

## Setup — one time, then never again

Assumes a **disposable dev stack, never one someone works in or one in
production use**. A demo uploads audio, creates meetings, can change settings on
the Configuration page and deletes its fixtures afterwards.

### 1. Install the browser MCP server (once)

```bash
claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@1.12.0 \
    --browserUrl http://127.0.0.1:9222 --redactNetworkHeaders
```

`--redactNetworkHeaders` matters: the session carries Cognito bearer tokens and
they would otherwise land in the transcript.

**Then restart Claude Code** — MCP servers load at session start, so the tools
are absent until it restarts.

### 2. Launch a debug Chrome with its own profile (once)

macOS:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.cache/chrome-lma-demo" \
    --no-first-run --no-default-browser-check >/dev/null 2>&1 &
```

Linux with a desktop session: the same flags on `google-chrome` (or `chromium`).

Verify before going further — this must return JSON, not 404 and not nothing:

```bash
curl -s http://127.0.0.1:9222/json/version
```

**The dedicated `--user-data-dir` is mandatory, not a preference.** Chrome 136 and
later **silently ignore `--remote-debugging-port` on the default profile**: Chrome
starts normally, but the port never answers and nothing explains why. This is a
deliberate Chrome hardening, so do not offer "reuse your normal profile" as an
option; it does not work on any current Chrome. The consequence, worth telling the
user up front: **they sign in to the stack once in this profile.** The profile
persists, so it is a one-time cost, not per-run.

### 3. Get the stack URL and sign in

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py url <STACK> --region <region>
```

`AWS_PROFILE=default` per CLAUDE.md; other profiles point at unrelated accounts.
The URL is the root stack's `ApplicationCloudfrontEndpoint` output. A stack is
invisible from the wrong region; if the name is not found the script lists the
stacks with LMA in their name that it can see.

Have the user open that URL in the debug Chrome and sign in as themselves. After
the first time the profile remembers it.

### 4. Recording tools

`make ux-record-deps` — ffmpeg and ffprobe (from the system: `brew install ffmpeg`
or the distribution's package) and boto3 and Pillow
(`pip install -r scripts/requirements-ux-recorder.txt`) must all be present.

### Personas

Demos are recorded as the Admin persona unless the story is about what a regular
user sees. LMA has two roles (`docs/user-management.md`): **Admin**, the `Admin`
Cognito group, and **User**, a user in no group, who sees only their own meetings
and those shared with them, and no Configuration section. To record as a User:

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py setup <STACK> --group User \
    --region <region>
# ... sign in as the printed user, prepare fixtures AS that user, record,
# then run the teardown command it prints
```

(`make ux-test-session STACK_NAME=<STACK> [REGION=…] [GROUP=User] [EMAIL_DOMAIN=…]`
is the same command.) This creates a real Cognito user with a known password in a live pool,
so **always run the teardown it prints** — nothing else expires that account;
`teardown <STACK> --stale` sweeps any whose teardown was never run. The stack's
app client is *not* modified: `admin-set-user-password --permanent` is a user-pool
admin API, and the browser signs in over SRP, which the UI client already allows.
A stack deployed with `AllowedSignUpEmailDomain` refuses addresses outside that
domain, including admin-created ones; pass `--email-domain <that domain>`. The
sweep matches only users on the domain it is given, so a user made that way is
swept by `teardown <STACK> --stale --email-domain <that domain>`.

A fresh User owns no meetings, so its fixtures have to be made while signed in as
it (or shared to it from the Admin account).

### Gotchas, all of them measured rather than assumed

- **Chrome 136+ ignores `--remote-debugging-port` on the default profile** —
  silently, so it looks like the flag worked. See step 2.
- **`--remote-debugging-port` has no effect on an already-running Chrome** either;
  a second launch hands off to the existing process and CDP stays off.
- **`chrome://inspect/#remote-debugging` does not work with this tool.** It opens
  the port but serves no HTTP discovery endpoints (`/json/version` → 404). Do not
  recommend it.
- **Never launch a second Chrome on a port another Chrome already holds.** They
  split across IPv4 and IPv6 on the same port number and the browser can hang.
  Check with `lsof -nP -iTCP:9222 -sTCP:LISTEN`.
- **The recorded tab must stay visible, unminimized and not covered**, with the
  tab in front. macOS stops compositing an occluded window and the screencast
  stops with it; `./scripts/ux_recorder.py status` says `tab visible False` when
  that happens. Do not resize the window mid-recording, and keep DevTools closed
  in that tab.
- If `list_pages` reports it cannot connect, re-check step 2 rather than trying
  other flags.

### LMA-specific things to know before driving the UI

- **The UI is hash-routed**: pages are `<url>#/calls`, `#/stream`, `#/upload`,
  `#/virtual-participant`, `#/configuration/settings`,
  `#/configuration/user-management` and so on (`lma-ai-stack/source/ui/src/routes/constants.js`).
- **Some side-navigation links open a new tab.** Virtual Participant, Stream Audio,
  Upload Audio and Enrol My Voice are external links in the navigation
  (`components/common/navigation-items.js`), and the recorder follows only the tab
  it attached to. Reach those pages in the recorded tab with the MCP
  `navigate_page` to the hash URL, or attach the recorder (`--target`) to the tab
  the page opens in. Rehearsal is where you find out which.
- **The side navigation shows the stack name.** The Deployment Info section lists
  Stack Name, Build and Version. Read the version from it in step 2, then collapse
  the section before `start` so the stack name is not in frame.
- **Browser permission prompts are not in the video.** The screencast is the page,
  not the browser's own UI, so the microphone and tab-share prompts of Stream
  Audio happen off camera. That is an inference from how the screencast works, not
  something measured on this page; check it in rehearsal.
- **Live capture needs a live source.** Stream Audio needs a microphone or a tab
  playing audio, and a Virtual Participant needs a real meeting it can join. Both
  are better recorded as a finished meeting plus a short live chapter than as a
  long live session.

---

## Where the material comes from

Look in `scripts/demo_storyboards.yaml` first. If a storyboard already covers the
request, offer to re-record it (updated for the current version) as one of the
three proposals — a demo that exists for the previous release is the one most worth
refreshing. Its first entry is a format example that has never been rehearsed;
treat it as a proposal, not as a tested script.

### A changelog section

Read `CHANGELOG.md` — `## [Unreleased]` by default, or the `## [X.Y.Z]` section the
user names. Most entries link the doc that explains them; read those links, that is
where the user-visible behaviour is described. Then sort the entries:

- **Shows in the browser** — a new page, panel, control, column, state or message
  in the web UI. Demoable as-is.
- **Shows only in artifacts** — a field in a transcript download, a line in the
  summary, a CloudWatch dashboard widget, a CLI flag, a stack parameter. Demoable
  through the page that displays it (the meeting page, the Configuration page),
  or not at all.
- **Invisible** — IAM scoping, a Lambda's memory, a retry window. Not demoable.
  Say so in the proposal rather than silently dropping it; the user may want a
  slide instead of a video.

### A PR

Fetch it the way `.claude/skills/pr-review.md` Step 1 does — `gh pr view`,
`gh pr diff` — but stop there. You are learning *what changed and why*, not
producing the six-question review. Read the PR description, the CHANGELOG hunk if
there is one, and the changed files under `lma-ai-stack/source/ui/` and `docs/`. A
PR with no change under either is usually in the "artifacts" or "invisible" bucket
above.

A PR's change is on the stack only if the stack was deployed from that branch or a
release that includes it. Compare the version and build time in Deployment Info
with the PR's head; if they do not match, report **blocked — not deployed** and
stop. Deploying is a separate task the user has to ask for.

### A named feature

"Demo meeting consent", "show how configuration profiles work". Read the feature's
page under `docs/` (`docs/INDEX.md` lists them) and, if one exists, the entry in
`docs/demo-videos.md` — an existing public video means the new one should show
what changed since, not repeat it.

---

## Proposing three storyboards

Three *different* demos, not three lengths of the same one. Good axes to vary:

- **One feature, deep** — the single most visible change, start to finish.
- **What's new in this version** — a 3–4 minute tour touching several entries,
  one chapter each.
- **A person's job** — someone getting something done (a meeting owner sharing a
  summary, an administrator changing a setting for everyone) where the new
  features appear as they are needed, not as a list.

For each proposal give: a working title, who it is for, the one-sentence hook, 4–7
chapters as one line each, the fixtures it needs and how they will be made (see
Fixtures below), an estimated running time (2–4 minutes is the target), and what it
deliberately leaves out.

Rules for a proposal to be honest:

- Everything in it is **deployed on the stack** (step 2) and **reachable in the
  browser** by a persona you can sign in as.
- Its fixtures are made from shipped or synthetic audio, unless the user has
  already agreed otherwise (see Privacy).
- A wait longer than about a minute is either done before recording or covered by
  `pause`. A proposal built around watching a meeting being summarized is a weak
  one.

Present them with one `AskUserQuestion`, three options, the outline as each
option's preview. Put your recommendation first and say why in a sentence. The
user may pick, combine, or bring their own; take whatever they say and move on.

---

## Writing the storyboard

Expand the pick into an entry in `scripts/demo_storyboards.yaml` — the header of
that file documents the fields. The parts that matter most:

- **`chapters[].say`** is the narration, one or two sentences each. It is spoken
  before the action lands, so it says what the person is about to do and why it
  matters, in first person plural and present tense: "We share the meeting with a
  colleague, who can read it but not change it." Name the feature the way the docs
  name it. Never say click, uid, stack, id, or an email.
- **`chapters[].do`** is what you will actually do in the browser, precise enough
  to rehearse from.
- **`takeaways`** are the 3–5 lines that become the end card. Write them as
  claims a viewer takes away, not as a table of contents.
- **`fixtures`** lists what must exist before recording and how you will make it.
- **`not_shown`** is the honest boundary — the entries you left out and why.

Show the storyboard to the user and ask once whether to record it as written. If
they have already said "just go", do not ask again.

A storyboard is the only file under version control a demo run touches. It holds no
meeting content, account ids or names, so it is safe to commit; commit it with the
change it demonstrates, or on its own if the demo is of a past release.

---

## Fixtures and rehearsal

### Making a finished meeting

Most demos open on a meeting that has already ended, with its transcript and
summary. Ways to make one, from simplest:

- **Upload Audio with the shipped call recording.** On the Upload Audio page
  (`#/upload`), upload `utilities/load-simulator/lma_load/fixtures/stereo-demo-call.wav`
  — a real two-speaker call, about five minutes, one speaker per channel — with a
  neutral meeting topic. Wait until the meeting is *Ended* and the summary has
  appeared. This is the default fixture.
- **Several voices on one channel**, to show speaker separation: synthesize each
  turn with a different Amazon Polly neural voice (16 kHz PCM, each turn above
  about 2.5 seconds), concatenate the turns into a WAV, and upload it with
  **Diarize speakers** checked. `integ-tests/voice_enrollment_probe.py`
  (`_synthesise`) shows the Polly call.
- **A meeting captured over the Stream Audio WebSocket**, as a browser would
  stream it: `integ-tests/ws_stream_probe.py` streams the same shipped WAV in real
  time. Call its `run_probe()` (the script's own `__main__` deletes the meeting
  afterwards, which a fixture does not want). It needs a Cognito user's
  credentials and `integ-tests/requirements.txt`; see `.claude/skills/integ-tests.md`.
- **A meeting with shared-screen key frames**: `integ-tests/desktop_video_probe.py`
  streams Polly-narrated synthetic slides with video and leaves the meeting in
  place, so the Shared Screen panel has frames to show.

Check two settings on the Configuration page first: **Retention › Delete the
transcript after the summary** must be Off (the default) or the transcript is gone
before you record, and a meeting made as the Admin is not visible to a User
persona unless shared.

### Rehearsal

Do this before `start`, every time:

1. **Create the fixtures** as above. The demo opens on the finished result; it
   does not watch the summary being written.
2. **Walk every chapter once, unrecorded.** Take a `take_snapshot` at each step so
   you know the element you will click and what the page does next. This is where
   you find the flash message that shifts the table, the link that opens a new
   tab, the modal that needs a scroll, the button that is disabled until something
   loads.
3. **Clear the state you disturbed.** Do a real page reload, not a hash
   navigation: flash messages survive route changes and will otherwise sit in your
   first chapter. Collapse Deployment Info. Then take a screenshot, not a DOM
   query, to confirm what is on screen before `start` — the side navigation's
   open state, in particular, is easy to misread from the DOM.
4. **Keep the context small.** `take_snapshot` of a long transcript costs
   thousands of tokens each time. Save it to a file and grep for the uid you need:
   `take_snapshot --filePath scratch/…/snap.txt` then
   `grep 'button "Share"' snap.txt`.

If a step **does not work** during rehearsal, the demo stops here. Report it as
functional breakage — *steps → observed → expected*, with the stack's version and
the persona — and do not record around it. A demo that hides a broken step is
worse than no demo.

---

## Privacy — say it once, before recording

A demo is made to be shared, and a recording of a live stack shows real meetings,
real meeting titles and transcripts, the signed-in user's email in the top
navigation, the stack name in Deployment Info, and whatever else is on screen. The
video does not show the page URL (the screencast is the page, not the browser
chrome), but nothing on the page is redacted: a password revealed by a
show-password toggle is in the video. Polly receives only the narration text you
wrote; frames never leave the machine.

The session directory holds more than the video. Every click is logged, with a
label, in `events.jsonl` and `timeline.json`, and `render` prints the same labels in
its click table and warnings. A click on a form field, an editable region
(`contenteditable`, or a `textbox`, `searchbox` or `spinbutton` role) or a
`combobox` is labelled by what names the field — its `aria-label` or
`aria-labelledby` text, its `<label>`, its placeholder, or its name, type or role,
never by text inside it — so a click on a filled password field is logged as the
field's label, not the password. Any other click is labelled by the page's own
text, which can repeat what was typed or chosen: the current choice on a dropdown's
trigger button (Cloudscape's Select trigger is a plain button), a date picker's
calendar button, a token for a chosen option or filter, or an autosuggest option
reading `Use: "…"` with the typed value in it. A click made while paused is logged
with no label and no position, and left out of the video and the click table, so a
`pause` around a sign-in keeps what was clicked out of the click log as well as the
video. The session also records the tab's address, in `session.json`,
`timeline.json` and `recorder.log`, and its title, in `session.json`.

Default to shipped and synthetic audio (see Fixtures). If the storyboard needs
anything else — a meeting already on the stack, a real recording — tell the user
exactly what will be in frame and ask once. If they confirm, record it and repeat
the caveat when you hand over the path. Everything lands under
`scratch/ux-recordings/` (gitignored); **never commit the session directory, the
video included, or attach any of it to a PR**, and do not paste `render`'s click
table or warnings into anything you share. The one exception is the finished
`demo.mp4` the user chooses to publish: the user, not you, drags it into a GitHub
comment box to get its asset URL for `docs/demo-videos.md` (step 5 under After
stop), and nothing else from the session goes with it.

---

## Recording

```bash
./scripts/ux_recorder.py targets                     # find the tab; MCP page ids are not these ids
./scripts/ux_recorder.py start --kind demo --stack <STACK> \
    --title "Sharing a meeting summary" --subtitle "Version 0.4.0" \
    --url-contains cloudfront \
    --say "<the hook — one or two sentences>"

# before EACH chapter:
./scripts/ux_recorder.py mark "<chapter label>" --say "<chapters[].say>"
#   ... then the MCP clicks / fills for that chapter

./scripts/ux_recorder.py pause      # around any wait you could not pre-empt
./scripts/ux_recorder.py resume

./scripts/ux_recorder.py stop --say "<the closing line: the takeaway and where to read more>"
```

`--kind demo` is what makes this a demo: no persona on the title card, the
`--title` and `--subtitle` lines instead of the stack name, a **Key takeaways** end
card read from `demo.md`, and `demo.mp4` / `demo.srt` as the outputs. The session
directory is `scratch/ux-recordings/demo-<title>-<timestamp>/`. Without `--kind
demo` the recorder makes a UX review instead (stack and persona on the title card,
the Findings of a `review.md` on the end card); this skill does not use that mode.

`targets` lists the debuggable tabs; the MCP's `list_pages` ids are **not** these
ids. Pick the tab by a URL fragment (`--url-contains cloudfront`) or by the id
`targets` prints (`--target`).

`mark` goes **before** the action: the renderer holds the frame until the narrator
has started, then the click lands. A mark without `--say` still starts a chapter.
Use the MCP `click` for anything the viewer should see happen — a programmatic
`element.click()` from `evaluate_script` fires no mousedown, so the cursor overlay
draws nothing and Cloudscape popovers ignore it. Scrolling a long transcript with
`scrollTo({behavior: 'smooth'})` from a script is fine and reads well on camera.
Keep the pace of a person watching, not of the model driving — one idea per
chapter, five to eight chapters, and `take_snapshot` (not screenshots) between
actions so nothing flashes on screen that the viewer should not see. The recorder
captures what the tab shows continuously, and every mark also takes a frame.

**Clicks are drawn where they landed.** The cursor overlay reports each mousedown
with its coordinates, the element under the pointer and whether that element was
interactive. In the video each click is shown as a ring and pointer composited onto
the **last frame before the click**, held for `--click-hold` seconds (0.7) before
the page changes — a single-page app reacts within tens of milliseconds, so a
marker drawn live would land on the destination page. A marker away from the
element you meant is therefore the automation missing, not a drawing error, and the
timeline names what it hit. `status` counts clicks that hit nothing interactive;
`render` prints the click table. In a demo, a click on the wrong element is a
re-take, not a footnote — re-record the whole demo (it is three minutes) rather
than ship a ring drawn on the page header.

### How the video is paced

The recorder receives a frame only when the screen changes, so your thinking time
between steps costs nothing. `render` clamps idle gaps, drops paused stretches,
holds each mark's frame while the voice starts, never fast-forwards past 3×, and
settles on the final state before the next chapter. Narration is Amazon Polly's
generative engine (Ruth by default; Matthew, Stephen, Danielle, Joanna, Salli,
Tiffany also work). Captions are embedded as an English subtitle track — toggled
in the player (QuickTime: View → Subtitles; VLC: Subtitle menu) — and written to
`demo.srt`; `--no-captions` leaves the track out.

### Narration style

The opening line is the hook: what the viewer will be able to do after watching.
Each chapter says the *why* before the *what*. The closing line states the
takeaway and names the doc to read. No stack names, ids, emails, selectors or the
word "click". No findings and no hedging — a demo shows what the product does; if
something needs a caveat, it goes in `not_shown` and the docs entry, not the voice
track.

---

## After stop

1. Fill in `<session>/demo.md` — the skeleton is there: **Storyboard** (chapter
   labels), **Key takeaways** (3–5 indented lines; `render` puts the first five on
   the end card), **Fixtures**, **Not shown**.
2. Optionally edit `<session>/narration.md`; only changed lines are re-synthesised.
3. `AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth --dry-run`
   first. It prints the pacing table: a chapter silent for more than a few
   seconds, or sitting at the 3× speed ceiling, wants a longer narration line or a
   `pause` next time. The narration can still be lengthened now: edit that
   chapter's line in `narration.md` to describe what the footage shows (the video
   is paced to the voice, so a longer line means less speed-up), then re-run the
   dry run.
4. `AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth`, then watch
   the mp4 length against the target and check the click table. Beside the mp4:
   `demo.srt`, `segments.json` (the chapter table, also written into `demo.md`)
   and `timeline.json`.
5. Write `<session>/docs-entry.md` — a ready-to-paste section for
   `docs/demo-videos.md` in the shape that page documents:

   ```markdown
   ### <Title>
   <One paragraph: what it shows and why it matters, in the voice of the existing entries.>

   **Duration**: ~<m> minutes

   <ASSET_URL — upload demo.mp4 by dragging it into a comment on a GitHub PR or issue in this repo, then paste the https://github.com/user-attachments/assets/... URL here>

   **Related Documentation**: [<Doc title>](./<doc>.md#<anchor>)
   ```

   Say which heading of `docs/demo-videos.md` it belongs under, or that it needs a
   new one (added to that page's table of contents). The upload itself is manual:
   GitHub only mints asset URLs from a comment box, so the skill drafts the entry
   and the user attaches the file. The README embeds one headline video at its
   top; whether a new demo replaces it is the user's decision, not yours.
6. Update the storyboard's `last_recorded` with the date and the version shown in
   Deployment Info.
7. Delete the fixtures you made (meetings, and any setting you changed), and run
   the `ux_test_session.py teardown` command if you created a user.

---

## Reporting

```
Demo — <title>, <date>
Source     CHANGELOG [Unreleased]: <entries> | PR #<NN> | <feature>
Stack      version <X.Y.Z> (build <date>), <persona>
Video      scratch/ux-recordings/demo-<...>/demo.mp4   <m:ss>, <N> chapters — not for commit
Captions   demo.srt   Chapters in demo.md
Docs entry scratch/ux-recordings/demo-<...>/docs-entry.md → docs/demo-videos.md, under "<heading>"
Storyboard scripts/demo_storyboards.yaml → <id>

Fixtures   <what was created, and that it was cleaned up>
Not shown  <entries left out, and why>
Seen on the way  <anything broken or odd during rehearsal, in steps → observed → expected form>
```

Repeat the privacy caveat if the recording shows anything beyond shipped or
synthetic fixtures.

## Don't

- **Don't record before rehearsing.** The first time a click lands must not be on
  camera.
- **Don't demo around a broken step.** Stop and report it.
- **Don't narrate findings.** The video shows; the docs entry and `not_shown`
  qualify.
- **Don't show what is not deployed.** Blocked is a valid result; invented is not.
- **Don't commit anything under `scratch/`**, and leave uploading the mp4 to the
  user (step 5 under After stop).
- **Don't hide, minimize or resize the recorded tab** while recording, and don't
  record with DevTools, a bearer token or the Deployment Info stack name in frame.
