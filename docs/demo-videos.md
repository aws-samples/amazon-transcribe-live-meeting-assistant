---
title: "Demo Videos"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Demo Videos

This page collects the demonstration videos for the Live Meeting Assistant (LMA), organized by feature area.

## Table of Contents

- [Overview](#overview)
- [Recording a new demo](#recording-a-new-demo)

---

## Overview

### Live Meeting Assistant overview
The project's demo of many of LMA's features, also shown at the top of the [README](../README.md).

https://github.com/user-attachments/assets/7642f659-ed9e-4abf-8baf-2f6fb27b08cb

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md)

---

## Recording a new demo

The repository includes a recorder that turns a browser session against a deployed stack into a narrated, captioned mp4 with title, chapter and takeaway cards. The procedure, from proposing a storyboard to drafting the entry for this page, is the [product demo skill](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/develop/.claude/skills/product-demo.md) (`.claude/skills/product-demo.md`), and the recorder is `scripts/ux_recorder.py`.

Videos on this page are attached as GitHub assets: drag the mp4 into a comment on a pull request or issue in this repository, then paste the resulting `https://github.com/user-attachments/assets/...` URL into the entry. Record against a disposable stack with synthetic or shipped audio; a recording shows whatever is on screen, unredacted. Upload only the mp4: the rest of its session directory, which logs clicks with text from the page, is never committed or uploaded.

A new entry goes under the heading of the feature area it shows, in this shape:

```markdown
### <Title>
<One paragraph: what it shows and why it matters.>

**Duration**: ~<m> minutes

<the https://github.com/user-attachments/assets/... URL>

**Related Documentation**: [<Doc title>](./<doc>.md#<anchor>)
```

Add a heading for a feature area that has no video yet, and list it in the table of contents.
