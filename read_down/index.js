#!/usr/bin/env node
/**
 * read_down — Readability + Turndown pipeline.
 *
 * CLI usage:
 *   echo '{"html":"...","url":"https://..."}' | node index.js
 *
 * Library usage:
 *   const { readDown } = require('./index.js');
 *   const result = readDown(html, { url, headingStyle: 'atx' });
 *
 * Interface aligned with hermes-sidebar PageExtractionResult.
 */

const { JSDOM } = require('jsdom');
const { Readability } = require('@mozilla/readability');
const TurndownService = require('turndown');
const { gfm } = require('turndown-plugin-gfm');

// ---------------------------------------------------------------------------
// Default Turndown rules (mirrors hermes-sidebar inline config)
// ---------------------------------------------------------------------------

const DEFAULT_TURNDOWN_OPTIONS = {
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
  linkStyle: 'inlined',
  hr: '---',
};

// Selectors to remove (matches hermes-sidebar exactly)
const REMOVE_SELECTORS = [
  'script', 'style', 'noscript', 'nav', 'footer', 'iframe',
];

// ---------------------------------------------------------------------------
// Debug logging
// ---------------------------------------------------------------------------

function debugLog(debug, ...args) {
  if (debug) {
    console.error('[read_down]', ...args);
  }
}

// ---------------------------------------------------------------------------
// Fallback HTML → Markdown (mirrors hermes-sidebar fallbackHtmlToMarkdown)
// ---------------------------------------------------------------------------

/**
 * Regex-based HTML→Markdown fallback. Preserves h1-6, strong, em, code,
 * links, paragraphs, line breaks, list items. Matches hermes-sidebar's
 * fallbackHtmlToMarkdown exactly.
 */
function fallbackHtmlToMarkdown(html) {
  let text = html
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
    .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, '')
    .replace(/<a[^>]*href="#\w*"[^>]*>(?:skip[^<]*)<\/a>/gi, '')
    .replace(/<h([1-6])[^>]*>([\s\S]*?)<\/h\1>/gi, (_m, n, t) =>
      '\n' + '#'.repeat(parseInt(n)) + ' ' + t.replace(/<[^>]*>/g, '').trim() + '\n',
    )
    .replace(/<strong[^>]*>([\s\S]*?)<\/strong>/gi, '**$1**')
    .replace(/<b[^>]*>([\s\S]*?)<\/b>/gi, '**$1**')
    .replace(/<em[^>]*>([\s\S]*?)<\/em>/gi, '_$1_')
    .replace(/<i[^>]*>([\s\S]*?)<\/i>/gi, '_$1_')
    .replace(/<code[^>]*>([\s\S]*?)<\/code>/gi, '`$1`')
    .replace(/<a[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi, '[$2]($1)')
    .replace(/<p[^>]*>([\s\S]*?)<\/p>/gi, '\n$1\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<li[^>]*>([\s\S]*?)<\/li>/gi, '- $1\n')
    .replace(/<[^>]*>/g, '');

  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .join('\n')
    .trim();
}

// ---------------------------------------------------------------------------
// Main pipeline
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} ReadDownResult
 * @property {string}  text          - Plain text (Readability textContent or fallback)
 * @property {string}  [markdown]    - Markdown output
 * @property {string}  [html]        - Article HTML (Readability content)
 * @property {string}  [title]       - Extracted article title
 * @property {string}  [byline]      - Author/byline
 * @property {string}  [dir]         - Content direction
 * @property {number}  [length]      - Article character length
 * @property {string}  [lang]        - Language code
 * @property {string}  [error]       - Error message if failed
 */

/**
 * @typedef {Object} ReadDownOptions
 * @property {string}  [url]             - Page URL (for JSDOM base URL resolution)
 * @property {boolean} [useReadability]  - Use Readability for article extraction (default: true)
 * @property {boolean} [debugTrace]      - Print debug info to stderr (default: false)
 * @property {string}  [headingStyle]    - Turndown headingStyle, 'atx' | 'setext' (default: 'atx')
 * @property {boolean} [skipTurndown]    - Return Readability HTML only, skip markdown
 * @property {string[]} [extraRemovals]  - Extra CSS selectors to remove before Turndown
 */

/**
 * Run Readability + Turndown on raw HTML.
 * Interface aligned with hermes-sidebar PageExtractionResult.
 *
 * @param {string} html     - Raw page HTML
 * @param {ReadDownOptions} [options]
 * @returns {ReadDownResult}
 */
function readDown(html, options = {}) {
  const { useReadability = true, debugTrace = false, skipTurndown = false } = options;

  const result = {
    text: '',
    markdown: undefined,
    html: undefined,
    title: undefined,
    byline: undefined,
    dir: undefined,
    length: undefined,
    lang: undefined,
    error: undefined,
  };

  if (!html || typeof html !== 'string' || !html.trim()) {
    result.error = 'empty-html';
    debugLog(debugTrace, 'empty HTML input');
    return result;
  }

  // --- Use Readability? ---
  if (!useReadability) {
    debugLog(debugTrace, 'Readability disabled, using raw extraction');
    return _rawExtract(html);
  }

  // --- Step 1: Readability (HTML → extracted article) ---
  debugLog(debugTrace, 'Step 1: Readability extraction starting...');

  let article;
  try {
    const dom = new JSDOM(html, {
      url: options.url || 'about:blank',
    });
    const doc = dom.window.document;

    debugLog(debugTrace, `  document.title = "${doc.title}"`);
    debugLog(debugTrace, `  document.documentElement.lang = "${doc.documentElement.lang || ''}"`);

    const reader = new Readability(doc, {
      debug: debugTrace,
      maxElemsToParse: 12000,
      charThreshold: 140,
      keepClasses: false,
    });
    article = reader.parse();
  } catch (err) {
    result.error = `readability-error: ${err.message}`;
    debugLog(debugTrace, '  Readability threw:', err.message);
    // Fallback: use raw extraction
    const fallback = _rawExtract(html);
    result.text = fallback.text;
    result.markdown = fallback.markdown;
    return result;
  }

  if (!article) {
    result.error = 'parse-returned-null';
    debugLog(debugTrace, '  Readability returned null, using raw extraction');
    const fallback = _rawExtract(html);
    result.text = fallback.text;
    result.markdown = fallback.markdown;
    return result;
  }

  result.text = article.textContent || '';
  result.html = article.content || undefined;
  result.title = article.title || undefined;
  result.byline = article.byline || undefined;
  result.dir = article.dir || undefined;
  result.length = article.length || undefined;
  result.lang = article.lang || undefined;

  debugLog(debugTrace, `  article title: "${article.title || ''}"`);
  debugLog(debugTrace, `  article length: ${article.length || 0} chars`);
  debugLog(debugTrace, `  article byline: ${article.byline || '(none)'}`);
  debugLog(debugTrace, 'Step 1 complete');

  // --- Step 2: Turndown (article HTML → Markdown) ---
  if (skipTurndown) {
    debugLog(debugTrace, 'Step 2: skipped (skipTurndown=true)');
    return result;
  }

  debugLog(debugTrace, 'Step 2: Turndown conversion starting...');

  if (!result.html) {
    // No article HTML to convert, use text as markdown
    result.markdown = result.text;
    debugLog(debugTrace, '  no article HTML, using text as markdown');
    return result;
  }

  try {
    const turndownOptions = {
      ...DEFAULT_TURNDOWN_OPTIONS,
      headingStyle: options.headingStyle || DEFAULT_TURNDOWN_OPTIONS.headingStyle,
    };
    const turndown = new TurndownService(turndownOptions);

    // Register GFM plugin (tables, strikethrough, task lists)
    turndown.use(gfm);

    // Remove noise elements (matches hermes-sidebar exactly)
    const removals = [...REMOVE_SELECTORS];
    if (options.extraRemovals && Array.isArray(options.extraRemovals)) {
      removals.push(...options.extraRemovals);
    }
    turndown.remove(removals);

    // Parse article HTML into DOM for Turndown
    const articleDom = new JSDOM(result.html);
    result.markdown = turndown.turndown(articleDom.window.document.body).trim();

    debugLog(debugTrace, `  markdown output: ${(result.markdown || '').length} chars`);
    debugLog(debugTrace, 'Step 2 complete');
  } catch (err) {
    result.error = `turndown-error: ${err.message}`;
    debugLog(debugTrace, '  Turndown threw:', err.message);
    // Fallback: markdown = plain text
    result.markdown = result.text;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Raw extraction (Readability disabled or failed)
// ---------------------------------------------------------------------------

function _rawExtract(html) {
  const result = {
    text: '',
    markdown: undefined,
    html: undefined,
    title: undefined,
    error: undefined,
  };

  try {
    const dom = new JSDOM(html);
    const doc = dom.window.document;

    result.text = doc.body.textContent || '';
    result.html = doc.documentElement.outerHTML || undefined;
    result.title = doc.title || undefined;

    // Run fallbackHtmlToMarkdown
    result.markdown = fallbackHtmlToMarkdown(result.html || html);
  } catch {
    // Dead last: strip tags
    result.text = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    result.markdown = result.text;
  }

  return result;
}

// ---------------------------------------------------------------------------
// CLI entry
// ---------------------------------------------------------------------------

if (require.main === module) {
  let input = '';
  process.stdin.setEncoding('utf-8');
  process.stdin.on('data', (chunk) => { input += chunk; });
  process.stdin.on('end', () => {
    try {
      const parsed = JSON.parse(input);
      const { html, url, options } = parsed;
      const opts = { ...(options || {}), url: url || options?.url };
      const result = readDown(html, opts);
      process.stdout.write(JSON.stringify(result));
    } catch (err) {
      process.stdout.write(JSON.stringify({
        text: '',
        error: `cli-error: ${err.message}`,
      }));
    }
  });
}

// ---------------------------------------------------------------------------
// Module exports
// ---------------------------------------------------------------------------

module.exports = { readDown, fallbackHtmlToMarkdown };
