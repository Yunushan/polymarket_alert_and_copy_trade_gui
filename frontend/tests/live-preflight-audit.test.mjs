import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const { LivePreflightAudit, livePreflightBlocked } = await import("../.test-dist/src/live-preflight-audit.js");

function payload(overrides = {}) {
  return {
    ok: true,
    blocked: false,
    message: "Preflight passed without executing an order.",
    order: {
      market_id: "polymarket",
      contract_id: "contract-1",
      side: "BUY",
      size: 2.5,
      limit_price: 0.4,
      approx_notional: 1,
      metadata_keys: ["forecast"]
    },
    preflight: {
      display_name: "Polymarket",
      feature: "live_trading",
      max_size: 10,
      max_notional: 20,
      warnings: [],
      requires_credentials: true,
      requires_kyc: true,
      region_limited: true
    },
    live_safety: {
      status: "ready",
      redaction: { audit_payloads_redacted: true }
    },
    ...overrides
  };
}

test("a healthy preflight is presented as passed with execution and redaction facts", () => {
  const html = renderToStaticMarkup(createElement(LivePreflightAudit, { payload: payload() }));

  assert.match(html, /data-preflight-result="passed"/);
  assert.match(html, />Result<\/span><strong>passed<\/strong>/);
  assert.match(html, />Market<\/span><strong>polymarket<\/strong>/);
  assert.match(html, />Metadata keys<\/span><strong>forecast<\/strong>/);
  assert.match(html, />Redaction<\/span><strong>enabled<\/strong>/);
  assert.match(html, /Preflight passed without executing an order\./);
});

test("contradictory failure data fails closed and exposes a safely escaped error", () => {
  const unsafeError = '<script>alert("credential")</script>';
  const failed = payload({
    ok: false,
    blocked: false,
    error: unsafeError,
    message: "",
    live_safety: {
      status: "blocked",
      redaction: { audit_payloads_redacted: false }
    }
  });
  const html = renderToStaticMarkup(createElement(LivePreflightAudit, { payload: failed }));

  assert.equal(livePreflightBlocked(failed), true);
  assert.match(html, /data-preflight-result="blocked"/);
  assert.match(html, /role="alert"/);
  assert.match(html, />Result<\/span><strong>blocked<\/strong>/);
  assert.match(html, />Redaction<\/span><strong>disabled<\/strong>/);
  assert.doesNotMatch(html, /<script>/i);
  assert.match(html, /&lt;script&gt;alert\(&quot;credential&quot;\)&lt;\/script&gt;/);
});
