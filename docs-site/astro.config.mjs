import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import remarkGithubVideo from "./plugins/remark-github-video.mjs";
import remarkRewriteDocsLinks from "./plugins/remark-rewrite-docs-links.mjs";

export default defineConfig({
  site: "https://aws-samples.github.io",
  base: "/amazon-transcribe-live-meeting-assistant",
  markdown: {
    remarkPlugins: [remarkGithubVideo, remarkRewriteDocsLinks],
  },
  integrations: [
    starlight({
      title: "LMA",
      description:
        "Live Meeting Assistant — Real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation on AWS",
      logo: {
        dark: "./src/assets/logo-dark.svg",
        light: "./src/assets/logo-light.svg",
        replacesTitle: false,
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant",
        },
      ],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Overview",
          items: [{ label: "Welcome", slug: "index" }],
        },
        {
          label: "Getting Started",
          items: [
            {
              label: "Prerequisites & Deployment",
              slug: "prerequisites-and-deployment",
            },
            { label: "Quick Start Guide", slug: "quick-start-guide" },
          ],
        },
        {
          label: "Core Features",
          items: [
            {
              label: "Transcription & Translation",
              slug: "transcription-and-translation",
            },
            { label: "Meeting Assistant", slug: "meeting-assistant" },
            {
              label: "Transcript Summarization",
              slug: "transcript-summarization",
            },
            { label: "Meetings Query Tool", slug: "meetings-query-tool" },
          ],
        },
        {
          label: "Meeting Sources",
          items: [
            { label: "Stream Audio", slug: "stream-audio" },
            { label: "Virtual Participant", slug: "virtual-participant" },
          ],
        },
        {
          label: "Voice Assistant & Avatar",
          items: [
            { label: "Voice Assistant", slug: "voice-assistant" },
            { label: "Nova Sonic Setup", slug: "nova-sonic-setup" },
            { label: "ElevenLabs Setup", slug: "elevenlabs-setup" },
            { label: "Simli Avatar Setup", slug: "simli-avatar-setup" },
          ],
        },
        {
          label: "MCP Server Integration",
          items: [
            { label: "MCP Servers Overview", slug: "mcp-servers" },
            { label: "Salesforce MCP", slug: "salesforce-mcp-setup" },
            {
              label: "Amazon QuickSuite MCP",
              slug: "quicksuite-mcp-setup",
            },
            { label: "DeepWiki MCP", slug: "deepwiki-mcp-setup" },
          ],
        },
        {
          label: "Web UI",
          items: [{ label: "Web UI Guide", slug: "web-ui-guide" }],
        },
        {
          label: "Access Control & Security",
          items: [
            {
              label: "User-Based Access Control",
              slug: "user-based-access-control",
            },
            {
              label: "Infrastructure & Security",
              slug: "infrastructure-and-security",
            },
          ],
        },
        {
          label: "Integration & API",
          items: [
            {
              label: "WebSocket Streaming API",
              slug: "websocket-streaming-api",
            },
            {
              label: "Embeddable Components",
              slug: "embeddable-components",
            },
            {
              label: "Lambda Hook Functions",
              slug: "lambda-hook-functions",
            },
          ],
        },
        {
          label: "Administration",
          items: [
            {
              label: "CloudFormation Parameters",
              slug: "cloudformation-parameters",
            },
            {
              label: "Stack Updates & Upgrades",
              slug: "stack-updates-and-upgrades",
            },
            { label: "Troubleshooting", slug: "troubleshooting" },
            { label: "Cleanup", slug: "cleanup" },
          ],
        },
        {
          label: "Development",
          items: [
            { label: "Developer Guide", slug: "developer-guide" },
            { label: "LMA CLI Reference", slug: "lma-cli" },
            { label: "LMA SDK Reference", slug: "lma-sdk" },
          ],
        },
      ],
    }),
  ],
  // Disable image optimization for content images (our docs reference ../images/ which are symlinked)
  image: {
    service: {
      entrypoint: "astro/assets/services/noop",
    },
  },
});
