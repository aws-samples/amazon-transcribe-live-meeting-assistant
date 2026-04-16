#!/usr/bin/env node
/**
 * sync-sidebar.mjs
 *
 * Detects docs/*.md files that are NOT listed in the manually‑curated sidebar
 * of astro.config.mjs and auto‑adds them to a "New & Uncategorized" section.
 *
 * Workflow:
 *   1. Scan docs/ for .md files → derive slugs (lowercase, no extension)
 *   2. Read astro.config.mjs, strip any existing auto‑generated section,
 *      and extract all *manually* placed sidebar slugs
 *   3. Identify docs that have no manual sidebar entry
 *   4. If any are missing → inject (or update) a "New & Uncategorized" section
 *      between marker comments so it can be re‑generated on the next run
 *   5. If none are missing → remove the auto section (user moved them all)
 *
 * Marker comments used in astro.config.mjs:
 *   // AUTO-SIDEBAR-START
 *   // AUTO-SIDEBAR-END
 *
 * Run:  node docs-site/sync-sidebar.mjs          (standalone)
 *       make docs-build                           (integrated)
 */

import { readdirSync, readFileSync, writeFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(__dirname, "..");
const configPath = join(__dirname, "astro.config.mjs");

const MARKER_START = "// AUTO-SIDEBAR-START";
const MARKER_END = "// AUTO-SIDEBAR-END";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Convert a slug like "agent-analysis" → "Agent Analysis" */
function slugToLabel(slug) {
  return slug
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// ---------------------------------------------------------------------------
// 1. Collect doc slugs from docs/
// ---------------------------------------------------------------------------

const docFiles = readdirSync(join(projectRoot, "docs"))
  .filter((f) => f.endsWith(".md") && f !== "README.md" && f !== "INDEX.md")
  .map((f) => f.replace(/\.md$/, "").toLowerCase())
  .sort();

// ---------------------------------------------------------------------------
// 2. Read config — strip auto section to get manual slugs only
// ---------------------------------------------------------------------------

const config = readFileSync(configPath, "utf-8");

const autoSectionRegex = new RegExp(
  `[ \\t]*${escapeRegex(MARKER_START)}[\\s\\S]*?${escapeRegex(MARKER_END)}[ \\t]*\\n?`,
  "m",
);
const manualConfig = config.replace(autoSectionRegex, "");

const manualSlugs = [...manualConfig.matchAll(/slug:\s*"([^"]+)"/g)].map(
  (m) => m[1],
);

// ---------------------------------------------------------------------------
// 3. Find missing docs
// ---------------------------------------------------------------------------

const missing = docFiles.filter((slug) => !manualSlugs.includes(slug));

// ---------------------------------------------------------------------------
// 4. Nothing missing — make sure auto section is removed and exit
// ---------------------------------------------------------------------------

if (missing.length === 0) {
  if (config !== manualConfig) {
    // Auto section existed but is no longer needed — clean it up
    writeFileSync(configPath, manualConfig);
    console.log(
      "🧹 Removed empty auto‑generated sidebar section (all docs are manually placed).",
    );
  }
  console.log("✅ All docs are included in the sidebar.");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// 5. Build the new auto section
// ---------------------------------------------------------------------------

console.log(`\n📄 Found ${missing.length} new doc(s) not yet in sidebar:`);
missing.forEach((slug) => console.log(`   • docs/${slug}.md`));

const items = missing
  .map(
    (slug) =>
      `            { label: "${slugToLabel(slug)}", slug: "${slug}" },`,
  )
  .join("\n");

const newSection = [
  `        ${MARKER_START}`,
  `        {`,
  `          label: "New & Uncategorized",`,
  `          items: [`,
  items,
  `          ],`,
  `        },`,
  `        ${MARKER_END}`,
].join("\n");

// ---------------------------------------------------------------------------
// 6. Patch config
// ---------------------------------------------------------------------------

let updatedConfig;

if (config.includes(MARKER_START)) {
  // Replace existing auto section in‑place
  updatedConfig = config.replace(autoSectionRegex, newSection + "\n");
} else {
  // First time — insert just before the sidebar array's closing "],"
  // That bracket is at 6‑space indent and is the LAST such occurrence.
  const lines = config.split("\n");
  let insertAt = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (/^\s{6}\],/.test(lines[i])) {
      insertAt = i;
      break;
    }
  }
  if (insertAt === -1) {
    console.error(
      "❌ Could not locate the sidebar closing bracket in astro.config.mjs",
    );
    process.exit(1);
  }
  lines.splice(insertAt, 0, newSection);
  updatedConfig = lines.join("\n");
}

writeFileSync(configPath, updatedConfig);

console.log(
  `\n✅ Auto‑added ${missing.length} doc(s) to "New & Uncategorized" sidebar section.`,
);
console.log(
  "   💡 Move them to the appropriate section in astro.config.mjs when ready.",
);
