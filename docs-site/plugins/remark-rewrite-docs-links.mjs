/**
 * Remark plugin that rewrites relative markdown links for the Starlight docs site.
 *
 * The docs in docs/ contain links like `[Quick Start](./quick-start-guide.md)` which
 * work when browsing files in the Git repo. On the Starlight site, pages are served
 * at directory-style URLs like `/quick-start-guide/` rather than `/quick-start-guide.md`.
 *
 * From a page like `/prerequisites-and-deployment/`, a link to `./quick-start-guide.md`
 * would resolve to `/prerequisites-and-deployment/quick-start-guide.md` (404). This
 * plugin rewrites it to `../quick-start-guide/` which correctly resolves to the sibling page.
 *
 * Transformations:
 *   ./some-doc.md           → ../some-doc/
 *   ./some-doc.md#section   → ../some-doc/#section
 *   some-doc.md             → ../some-doc/
 *   ../README.md            → https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/README.md
 */
import { visit } from "unist-util-visit";

const GITHUB_REPO =
  "https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant";

export default function remarkRewriteDocsLinks() {
  return (tree) => {
    visit(tree, "link", (node) => {
      const url = node.url;

      // Skip absolute URLs, anchors-only, and non-.md links
      if (!url || url.startsWith("http") || url.startsWith("#")) return;
      if (!url.includes(".md")) return;

      // Handle ../README.md or similar project-root links
      if (url.startsWith("../")) {
        // Links going outside docs/ — point to GitHub repo
        const path = url.replace(/^\.\.\//, "");
        node.url = `${GITHUB_REPO}/blob/main/${path}`;
        return;
      }

      // Handle ./doc.md, ./doc.md#anchor, or bare doc.md
      const match = url.match(
        /^(?:\.\/)?([a-zA-Z0-9_-]+)\.md(#[a-zA-Z0-9_-]*)?$/
      );
      if (match) {
        const slug = match[1].toLowerCase();
        const anchor = match[2] || "";
        node.url = `../${slug}/${anchor}`;
      }
    });
  };
}
