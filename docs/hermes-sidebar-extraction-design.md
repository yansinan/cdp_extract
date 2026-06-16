# hermes-sidebar Extraction Design Notes

Session: GitHub MCP setup + hermes-sidebar code analysis (2026-05-28)
Source: yansinan/hermes-sidebar (Chrome Side Panel extension)

## Files Read

### `src/shared/extractPageMainContent.ts`

- Uses `chrome.scripting.executeScript({ target: { tabId }, files: ["readability.bundle.js"] })`
- IIFE injects `window.Readability` into tab context
- 3-layer fallback: Readability → raw `document.body.innerText` → error marker
- Clones `document.body` into a new `createHTMLDocument` to avoid mutating live page
- Readability parse options: `{ maxElemsToParse: 12000, charThreshold: 140, keepClasses: false }`
- Returns: `{ text, html, title, byline, dir, length, lang, error }`

### `src/shared/htmlToMarkdown.ts`

- Uses `chrome.scripting.executeScript({ target: { tabId }, files: ["turndown.bundle.js"] })`
- IIFE exposes `window.createTurndownService(options)` factory (GFM plugin pre-applied)
- Regex-based `fallbackHtmlToMarkdown()` as safety net (strips tags, basic conversion)
- Turndown config: `{ headingStyle: "atx", codeBlockStyle: "fenced", bulletListMarker: "-", linkStyle: "inlined", hr: "---" }`
- Auto-removes: `["script", "style", "noscript", "nav", "footer", "iframe"]`
- Returns: markdown string

### Bundle Build (package.json)

```json
"build:readability": "esbuild src/readability-bundle.ts --bundle --format=iife --global-name=Readability --outfile=public/readability.bundle.js --minify",
"build:turndown": "esbuild src/turndown-bundle.ts --bundle --format=iife --outfile=public/turndown.bundle.js --minify",
"build:markdown": "esbuild scripts/markdown-bundle.entry.mjs --bundle --format=iife --global-name=HermesMarkdownBundle --outfile=public/markdown.bundle.js --minify"
```

## Dependencies (package.json)

- `@mozilla/readability ^0.6.0`
- `turndown ^7.2.1`
- `turndown-plugin-gfm ^1.0.2`
- Build: `esbuild ^0.28.0`

## Hermes Agent CDP Side (Current State)

Browser snapshot in Hermes uses **accessibility tree** (ariaSnapshot), not DOM/HTML path.
No Readability or Turndown used on CDP side.
CDP can evaluate arbitrary JS via:
- `browser_cdp({ method: "Runtime.evaluate", params: { expression: "..." } })`
- `browser_console({ expression: "document.documentElement.outerHTML" })`

## Key Design Decision

The convergence point is the **HTML string**:
- Extension: gets HTML from `executeScript` → passes to pure function
- CDP: gets HTML from `Runtime.evaluate` → passes to pure function
- Library: `extractHtmlToMarkdown(html, baseUrl?, options?)` — pure function, no browser APIs
