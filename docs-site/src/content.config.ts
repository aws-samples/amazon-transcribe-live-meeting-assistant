import { defineCollection, z } from "astro:content";
import { docsSchema } from "@astrojs/starlight/schema";
import { glob } from "astro/loaders";

export const collections = {
  docs: defineCollection({ schema: docsSchema() }),
  // Explicitly define lma-ai-stack so Astro doesn't auto-generate it
  // (this directory only contains symlinked images for ../lma-ai-stack/images/ refs)
  "lma-ai-stack": defineCollection({
    loader: glob({ pattern: "**/*.md", base: "src/content/lma-ai-stack" }),
    schema: z.object({}),
  }),
};
