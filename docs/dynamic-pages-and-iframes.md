# Dynamic Pages & Iframe Handling

Analysis from session 2026-05-28 — investigating whether a pure-function `extractHtmlToMarkdown(html)` library (Readability → Turndown) can handle SPAs, dynamic content, and iframes.

## Core Principle: Two-Layer Architecture

The library is a **pure function** that only processes HTML strings. All page-interaction concerns belong to the **caller (acquisition layer)**.

```
Acquisition Layer (caller)           │  Processing Layer (library)
────────────────────────────          │  ──────────────────────────
- Navigate to URL                     │  extractHtmlToMarkdown(html)
- Wait for JS rendering               │    ↓ Readability (denoise)
- Poll for content selector           │    ↓ Turndown (html→md)
- Grab outerHTML from each frame      │    ↓ return {markdown, title, ...}
- Pass html string(s) to library ──►  │
```

## Dynamic Pages / SPA

### What the library sees

- **CSR-only SPA (React/Vue root):** `<div id="root"></div>` — no content
- **Hydrated SPA:** Full DOM (works perfectly)
- **Infinite scroll / lazy load:** Only what's loaded at snapshot time

### Caller-side mitigations

**CDP — wait for rendering:**
```js
// Simple delay
document.documentElement.outerHTML
await new Promise(r => setTimeout(r, 2000))
document.documentElement.outerHTML  // re-grab

// Poll for content marker
void async function() {
  while(!document.querySelector('.article-content')) {
    await new Promise(r => setTimeout(r, 200));
  }
  return document.documentElement.outerHTML;
}()
```

**CDP — wait for network idle (via browser_cdp):**
```js
// Navigate, then poll frames until network is quiet
// Use Page.lifecycleEvent (networkIdle) via CDP session
```

**Extension — same via scripting.executeScript:**
```js
// The injected func() can be async — executeScript resolves when the promise settles
scripting.executeScript({
  target: { tabId },
  func: async () => {
    await new Promise(r => setTimeout(r, 2000));
    return document.documentElement.outerHTML;
  }
});
```

### What NOT to do

- ❌ Don't make the library handle wait/render logic — it violates the pure function contract
- ❌ Don't use `browser_snapshot` for extraction — it returns ariaSnapshot, not DOM HTML
- ❌ Don't assume `outerHTML` includes iframe content — it never does

## Iframe Content

### The Problem

`document.documentElement.outerHTML` of the **top-level frame** contains:
```
<html>
  <body>
    <h1>Page title</h1>
    <iframe src="https://example.com/widget"></iframe>  ← no inner content
    <p>Footer</p>
  </body>
</html>
```

The `<iframe>` element's tag is included, but its contentDocument is a separate DOM tree not serialized by outerHTML.

### Same-Origin Iframes

Accessible from the parent frame:
```js
// From the top-level context (via eval or injected script)
const iframes = document.querySelectorAll('iframe');
Array.from(iframes).map(f => ({
  label: f.title || f.id || 'unnamed',
  html: f.contentDocument?.documentElement?.outerHTML || null
}));
```

### Cross-Origin (OOPIF) Iframes

`contentDocument` is `null` due to same-origin policy. Must use CDP's frame-scoped evaluation:

1. Get `frame_id` from `browser_snapshot().frame_tree.children[]` where `is_oopif=true`
2. Call `browser_cdp({ method: "Runtime.evaluate", params: { expression: "document.documentElement.outerHTML" }, frame_id })`

### Merging Strategies

**Option A: Caller merges (recommended for clarity)**
```
1. Grab main HTML (top frame)
2. For each iframe, grab its HTML separately
3. Pass each through extractHtmlToMarkdown()
4. Concatenate: mainMarkdown + "\n\n---\n\n" + iframeMarkdowns.join("\n\n---\n\n")
```

**Option B: Library-parameter merge**
```typescript
interface ExtractOptions {
  iframeHtmls?: Array<{
    html: string;
    label?: string;       // e.g. "Sidebar Widget" — used as heading
  }>;
  // Library processes each and appends under a "## {label}" heading
}
```

## Summary

| Scenario | Library handles? | Caller must handle? |
|----------|-----------------|---------------------|
| Static HTML page | ✅ Fully | Nothing |
| Hydrated SPA | ✅ Fully | Wait for hydration before grabbing HTML |
| CSR-only SPA | ⚠️ Input is `<div id="root">` | Wait for JS render before grabbing HTML |
| Infinite scroll | ⚠️ Only visible content | Scroll first, then grab HTML |
| Same-origin iframes | ⚠️ Needs separate call per iframe | Collect iframe HTMLs, pass individually |
| Cross-origin iframes | ⚠️ Needs CDP frame_id | Use CDP frame-scoped eval per iframe |
