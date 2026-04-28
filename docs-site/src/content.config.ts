import { defineCollection, z } from "astro:content";
import { docsLoader } from "@astrojs/starlight/loaders";
import { docsSchema } from "@astrojs/starlight/schema";

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
  // Explicitly define lma-ai-stack to prevent Astro auto-discovery warning.
  // This directory only contains symlinked images (no docs) — used for
  // ../lma-ai-stack/images/ relative paths in content files.
  "lma-ai-stack": defineCollection({
    loader: () => [],
    schema: z.object({}),
  }),
};
