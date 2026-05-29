#!/usr/bin/env node
/**
 * read_down — Readability + Turndown pipeline.
 *
 * CLI usage:
 *   echo '{"html":"...","url":"https://..."}' | node index.js
 *
 * Library usage:
 *   const { readDown } = require('./index.js');
 *   const result = await readDown(html, { url, headingStyle: 'atx' });
 */

const { JSDOM } = require('jsdom');
const { Readability } = require('@mozilla/readability');
const TurndownService = require('turndown');
const { gfm } = require('turndown-plugin-gfm');

// ---------------------------------------------------------------------------
// Default Turndown rules: mirrors hermes-sidebar's inline config + GFM
// ---------------------------------------------------------------------------

const DEFAULT_TURNDOWN_OPTIONS = {
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
  linkStyle: 'inlined',
  hr: '---',
};

// Selectors to remove entirely (noise elements)
const REMOVE_SELECTORS = [
  'script', 'style', 'noscript', 'nav', 'footer',
  'iframe', '.sidebar', '.advertisement', '.ad',
];

// ---------------------------------------------------------------------------
// Main pipeline
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} ReadDownResult
 * @property {string}  markdown - Markdown output (Turndown result)
 * @property {string}  text     - Plain text (Readability textContent)
 * @property {string}  html     - Article HTML (Readability content)
 * @property {string}  [title]  - Extracted article title
 * @property {string}  [byline] - Author/byline
 * @property {string}  [dir]    - Content direction
 * @property {number}  [length] - Article character length
 * @property {string}  [lang]   - Language code
 * @property {string}  [error]  - Error message if failed
 */

/**
 * @typedef {Object} ReadDownOptions
 * @property {string}  [url]             - Page URL (for relative link resolution)
 * @property {string}  [headingStyle]    - 'atx' | 'setext' (default: 'atx')
 * @property {boolean} [skipTurndown]    - Return Readability HTML only, skip markdown
 * @property {string[]} [extraRemovals]  - Extra CSS selectors to remove before Turndown
 */

/**
 * Run Readability + Turndown on raw HTML.
 *
 * @param {string} html     - Raw page HTML
 * @param {ReadDownOptions} [options]
 * @returns {ReadDownResult}
 */
function readDown(html, options = {}) {
  const result = {
    markdown: '',
    text: '',
    html: '',
    title: undefined,
    byline: undefined,
    dir: undefined,
    length: undefined,
    lang: undefined,
    error: undefined,
  };

  if (!html || typeof html !== 'string' || !html.trim()) {
    result.error = 'empty-html';
    return result;
  }

  // --- Step 1: Readability (HTML → article) ---
  let article;
  try {
    const dom = new JSDOM(html, {
      url: options.url || 'about:blank',
    });
    const doc = dom.window.document;
    const reader = new Readability(doc, {
      debug: false,
      maxElemsToParse: 12000,
      charThreshold: 140,
      keepClasses: false,
    });
    article = reader.parse();
  } catch (err) {
    result.error = `readability-error: ${err.message}`;
    // Fallback: use raw body text
    try {
      const dom = new JSDOM(html);
      result.text = dom.window.document.body.textContent || '';
    } catch {
      result.text = stripHtml(html);
    }
    return result;
  }

  if (!article) {
    result.error = 'readability-returned-null';
    // Fallback to raw body
    try {
      const dom = new JSDOM(html);
      result.text = dom.window.document.body.textContent || '';
      result.html = dom.window.document.body.innerHTML || '';
    } catch {
      result.text = stripHtml(html);
    }
    return result;
  }

  result.text = article.textContent || '';
  result.html = article.content || '';
  result.title = article.title || undefined;
  result.byline = article.byline || undefined;
  result.dir = article.dir || undefined;
  result.length = article.length || undefined;
  result.lang = article.lang || undefined;

  // --- Step 2: Turndown (article HTML → Markdown) ---
  if (!options.skipTurndown && result.html) {
    try {
      const turndown = new TurndownService({
        ...DEFAULT_TURNDOWN_OPTIONS,
        headingStyle: options.headingStyle || DEFAULT_TURNDOWN_OPTIONS.headingStyle,
      });

      // Register GFM plugin (tables, strikethrough, task lists)
      turndown.use(gfm);

      // Remove noise elements
      const removals = [...REMOVE_SELECTORS];
      if (options.extraRemovals && Array.isArray(options.extraRemovals)) {
        removals.push(...options.extraRemovals);
      }
      turndown.remove(removals);

      // Parse article HTML into DOM for Turndown
      const articleDom = new JSDOM(result.html);
      result.markdown = turndown.turndown(articleDom.window.document.body).trim();
    } catch (err) {
      result.error = `turndown-error: ${err.message}`;
      // Fallback: markdown = plain text
      result.markdown = result.text;
    }
  } else if (!options.skipTurndown && !result.html) {
    result.markdown = result.text;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function stripHtml(text) {
  return text
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
    .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
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
        markdown: '',
        text: '',
        html: '',
        error: `cli-error: ${err.message}`,
      }));
    }
  });
}

// ---------------------------------------------------------------------------
// Module exports
// ---------------------------------------------------------------------------

module.exports = { readDown };
