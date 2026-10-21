import { existsSync, statSync } from "node:fs";
import path from "node:path";
import { defineConfig } from "vitepress";
import llmstxt from "vitepress-plugin-llms";
import { withMermaid } from "vitepress-plugin-mermaid";

// The docs site: docs/ is its source, and every page stays plain Markdown that also reads on
// github.com. GitHub Pages serves it under the repo's name; DOCS_BASE=/ builds it for a host
// at the root (a preview).
const REPO = "https://github.com/Eliav2/openclaw-openwebui-integration";
const base = process.env.DOCS_BASE ?? "/openclaw-openwebui-integration/";
const DOCS = path.resolve(__dirname, "..");

// A link to a file outside docs/ (the functions, SECURITY.md, skills/) works on GitHub, where
// the whole repo is there. On the site it points at the file on GitHub instead.
function repoLinks(md) {
  md.core.ruler.after("inline", "repo-links", (state) => {
    const from = path.dirname(path.join(DOCS, state.env.relativePath ?? ""));
    const visit = (tokens) => {
      for (const t of tokens) {
        if (t.children) visit(t.children);
        if (t.type !== "link_open") continue;
        const href = t.attrGet("href");
        if (!href || /^([a-z]+:|#|\/)/i.test(href)) continue;
        const [file, frag] = href.split("#");
        const target = path.resolve(from, file);
        if (target.startsWith(DOCS + path.sep)) continue;
        const repoPath = path.relative(path.dirname(DOCS), target);
        const kind = existsSync(target) && statSync(target).isDirectory() ? "tree" : "blob";
        t.attrSet("href", `${REPO}/${kind}/main/${repoPath}${frag ? `#${frag}` : ""}`);
      }
    };
    visit(state.tokens);
  });
}

// A demo is a GIF in the Markdown, which is what github.com can show. The site plays the same
// recording as a muted, looping video instead (sharper and smaller), when record.py wrote its
// MP4 and WebM next to the GIF. The GIF stays inside the <video> for a browser without either.
const DEMO_IMG = /<img\b[^>]*\bsrc="([^"]*\/demos\/([\w-]+))\.gif"[^>]*>/g;

function demoVideos(md) {
  md.core.ruler.push("demo-videos", (state) => {
    const from = path.dirname(path.join(DOCS, state.env.relativePath ?? ""));
    const swap = (html) =>
      html.replace(DEMO_IMG, (img, stem) => {
        const has = (ext) => existsSync(path.resolve(from, `${stem}.${ext}`));
        if (!has("mp4") || !has("webm")) return img;
        const width = /\bwidth="(\d+)"/.exec(img)?.[1] ?? "800";
        const alt = /\balt="([^"]*)"/.exec(img)?.[1] ?? "";
        return (
          `<video class="demo" width="${width}" autoplay muted loop playsinline aria-label="${alt}">` +
          `<source src="${stem}.webm" type="video/webm"><source src="${stem}.mp4" type="video/mp4">${img}</video>`
        );
      });
    const visit = (tokens) => {
      for (const t of tokens) {
        if (t.children) visit(t.children);
        if (t.type === "html_block" || t.type === "html_inline") t.content = swap(t.content);
      }
    };
    visit(state.tokens);
  });
}

// The home page: docs/index.md is the list of every page on GitHub; on the site it opens with a
// hero and the feature cards, and the list follows them.
const home = {
  layout: "home",
  hero: {
    name: "OpenClaw for Open WebUI",
    text: "Watch your agents work",
    tagline: "Chat with your OpenClaw agents from Open WebUI and watch them work: tool calls as cards, questions as dialogs, sub-agents in a live panel.",
    image: { src: "/logo.svg", alt: "The crab claw" },
    actions: [
      { theme: "brand", text: "Get started", link: "/install/quickstart" },
      { theme: "alt", text: "Features", link: "/features/" },
      { theme: "alt", text: "GitHub", link: REPO },
    ],
  },
  features: [
    { title: "Live tool cards", details: "Every command, read and search the agent runs, as a card that fills in as it runs.", link: "/features/chat#tool-cards" },
    { title: "Questions as dialogs", details: "When the agent asks, you get a card with options or a secret field, not a wall of text.", link: "/features/questions" },
    { title: "Status", details: "Context used, rate limits, background work and sub-agents, one tap from the chat.", link: "/features/status" },
    { title: "Every Gateway model", details: "The model picker lists the models your Gateway allows, one session per chat.", link: "/features/sessions-and-models" },
    { title: "Replies from the agent", details: "Cron jobs, heartbeats and sub-agent results reply in the chat, live.", link: "/features/agent-turns" },
    { title: "Survives restarts", details: "Reloads, reconnects and Gateway restarts keep the reply and pick it up again.", link: "/features/reliability" },
  ],
};

// withMermaid renders ```mermaid blocks (the Architecture diagram) as SVG, in the site's light or dark theme.
export default withMermaid(defineConfig({
  title: "OpenClaw for Open WebUI",
  description: "Chat with your OpenClaw agents from Open WebUI and watch them work: tool calls as cards, questions as dialogs, sub-agents in a live panel.",
  base,
  cleanUrls: true,
  head: [["link", { rel: "icon", type: "image/svg+xml", href: `${base}logo.svg` }]],
  markdown: {
    config(md) {
      repoLinks(md);
      demoVideos(md);
    },
  },
  transformPageData(page) {
    if (page.relativePath === "index.md") Object.assign(page.frontmatter, home);
  },
  // docs/examples/ holds pages about our own setup, not the Pipe: reachable only from the
  // section that links them, never listed (sidebar, home page, search or llms.txt).
  vite: { plugins: [llmstxt({ ignoreFiles: ["examples/**"] })] },
  // The plugin switches to Mermaid's dark theme in dark mode. SVG text labels, not HTML ones: the
  // docs' paragraph styles would make an HTML label taller than Mermaid measured it, clipping its last line.
  mermaid: { theme: "neutral", fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif", htmlLabels: false, flowchart: { htmlLabels: false } },
  themeConfig: {
    logo: "/logo.svg",
    siteTitle: "OpenClaw for Open WebUI",
    search: {
      provider: "local",
      options: {
        _render(src, env, md) {
          const html = md.render(src, env);
          return env.relativePath.startsWith("examples/") ? "" : html;
        },
      },
    },
    socialLinks: [{ icon: "github", link: REPO }],
    editLink: { pattern: `${REPO}/edit/main/docs/:path`, text: "Edit this page on GitHub" },
    outline: { level: [2, 3] },
    nav: [
      { text: "Getting started", link: "/install/quickstart", activeMatch: "^/(why|install/)" },
      { text: "Features", link: "/features/", activeMatch: "^/features/" },
      { text: "Configuration", link: "/configuration" },
      { text: "Troubleshooting", link: "/troubleshooting" },
      { text: "Versions", link: "/versions" },
    ],
    sidebar: [
      {
        text: "Getting started",
        items: [
          { text: "Why this exists", link: "/why" },
          { text: "Quick start", link: "/install/quickstart" },
          { text: "The installer", link: "/install/installer" },
          { text: "Manual install", link: "/install/manual" },
          { text: "Device pairing", link: "/install/pairing" },
          { text: "Two hosts", link: "/install/two-hosts" },
          { text: "Status button", link: "/install/status-action" },
          { text: "Thinking control", link: "/install/thinking-filter" },
          { text: "Agent skills", link: "/install/agent-skills" },
          { text: "Update and uninstall", link: "/install/update-uninstall" },
        ],
      },
      {
        text: "Features",
        items: [
          { text: "All features", link: "/features/" },
          { text: "Chat", link: "/features/chat" },
          { text: "Questions", link: "/features/questions" },
          { text: "Status", link: "/features/status" },
          { text: "Sessions and models", link: "/features/sessions-and-models" },
          { text: "Replies from the agent", link: "/features/agent-turns" },
          { text: "Reliability", link: "/features/reliability" },
          { text: "Owner users", link: "/features/owner-users" },
        ],
      },
      {
        text: "Configuration",
        items: [
          { text: "Valves", link: "/configuration" },
          { text: "Security", link: "/security" },
        ],
      },
      {
        text: "Help",
        items: [
          { text: "Troubleshooting", link: "/troubleshooting" },
          { text: "Versions", link: "/versions" },
        ],
      },
      {
        text: "Contributing & internals",
        collapsed: true,
        items: [
          { text: "Architecture", link: "/architecture" },
          { text: "Development", link: "/development" },
          { text: "Metadata contract", link: "/metadata-contract" },
          { text: "Rehydration and persistence", link: "/rehydration-persistence" },
          { text: "Design: agent turns", link: "/design/agent-turns" },
          { text: "Design: ask-user modal", link: "/design/ask-user-modal" },
        ],
      },
    ],
    footer: { message: "Released under the MIT License.", copyright: "Docs source: docs/ in the repo" },
  },
}));
