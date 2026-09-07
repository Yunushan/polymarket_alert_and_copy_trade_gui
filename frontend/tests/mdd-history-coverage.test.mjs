import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const { MddHistoryCoverage } = await import("../.test-dist/src/mdd-history-coverage.js");

test("source caps and observed windows remain distinct from account verification", () => {
  const html = renderToStaticMarkup(createElement(MddHistoryCoverage, { payload: {
    mdd_history_status: "limit_reached",
    mdd_history_coverage: {
      closed_positions: { status: "limit_reached", returned: 500, limit: 500, first_timestamp: 100, last_timestamp: 200 }
    }
  } }));
  assert.match(html, /data-mdd-history="limit_reached"/);
  assert.match(html, /account equity unverified/);
  assert.match(html, /500 \/ 500/);
  assert.match(html, /1970-01-01T00:01:40.000Z/);
  assert.match(html, /limit reached/);
});

test("legacy results do not acquire invented history completeness", () => {
  const html = renderToStaticMarkup(createElement(MddHistoryCoverage, { payload: {} }));
  assert.match(html, /History coverage unverified/);
  assert.match(html, /data-mdd-history="unknown"/);
});

test("invalid timestamps render as unknown and source labels are escaped", () => {
  const html = renderToStaticMarkup(createElement(MddHistoryCoverage, { payload: {
    mdd_history_coverage: {
      "<script>unsafe</script>": { status: "end_of_results", returned: 1, limit: 500, first_timestamp: Infinity, last_timestamp: null }
    }
  } }));
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /Unknown/);
});

test("invalid financial history exposes rejected-row diagnostics without unsafe markup", () => {
  const html = renderToStaticMarkup(createElement(MddHistoryCoverage, { payload: {
    mdd_history_status: "invalid_source_data",
    mdd_source_quality: { status: "invalid", sources: {
      closed_positions: { status: "invalid", rows: 2, invalid_rows: 1, reasons: { invalid_timestamp: 1 } },
      "<script>unsafe</script>": { status: "invalid", rows: 1, invalid_rows: 1, reasons: { "<img src=x>": 1 } },
      trade_rows: { status: "valid", rows: 5, invalid_rows: 0, reasons: {} }
    } }
  } }));
  assert.match(html, /role="alert"/);
  assert.match(html, /Risk unavailable: invalid source data/);
  assert.match(html, /closed positions: 1 \/ 2 invalid rows/);
  assert.match(html, /invalid timestamp: 1/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img "), false);
  assert.equal(html.includes("trade rows:"), false);
});
