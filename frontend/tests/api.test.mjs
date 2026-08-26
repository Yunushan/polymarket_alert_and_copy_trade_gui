import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalCryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
const originalFetchDescriptor = Object.getOwnPropertyDescriptor(globalThis, "fetch");

Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: { location: { port: "5173" } }
});

const api = await import("../.test-dist/src/api.js");

function restoreProperty(name, descriptor) {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
  } else {
    delete globalThis[name];
  }
}

function useUuidSequence(...values) {
  let index = 0;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      randomUUID() {
        const value = values[index];
        index += 1;
        assert.ok(value, "test exhausted its deterministic UUID sequence");
        return value;
      }
    }
  });
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return payload;
    }
  };
}

afterEach(() => {
  restoreProperty("crypto", originalCryptoDescriptor);
  restoreProperty("fetch", originalFetchDescriptor);
});

after(() => {
  restoreProperty("window", originalWindowDescriptor);
});

test("ambiguous mutation failures reuse a canonical idempotency key until success", async () => {
  useUuidSequence("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002");
  const calls = [];
  let attempt = 0;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    attempt += 1;
    if (attempt === 1) {
      throw new TypeError("connection reset after request transmission");
    }
    return jsonResponse(200, { stored: true });
  };

  const firstPayload = { label: "reconcile-me", report: { beta: 2, alpha: 1 } };
  const reorderedPayload = { report: { alpha: 1, beta: 2 }, label: "reconcile-me" };
  await assert.rejects(api.storePolymarketLiveValidationReport(firstPayload), TypeError);
  await api.storePolymarketLiveValidationReport(reorderedPayload);
  await api.storePolymarketLiveValidationReport(firstPayload);

  assert.equal(calls[0].url, "http://127.0.0.1:8765/api/polymarket/live-validation/reports");
  assert.equal(calls[0].options.headers["Idempotency-Key"], calls[1].options.headers["Idempotency-Key"]);
  assert.notEqual(calls[1].options.headers["Idempotency-Key"], calls[2].options.headers["Idempotency-Key"]);
});

test("terminal structured 4xx errors fall back to HTTP status and rotate the next key", async () => {
  useUuidSequence("00000000-0000-4000-8000-000000000003", "00000000-0000-4000-8000-000000000004");
  const keys = [];
  let attempt = 0;
  globalThis.fetch = async (_url, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    attempt += 1;
    return attempt === 1
      ? jsonResponse(422, { error: { code: "INVALID_REPORT", message: "schema rejected", details: { field: "mode" } } })
      : jsonResponse(200, { stored: true });
  };

  await assert.rejects(
    api.storePolymarketLiveValidationReport({ label: "invalid-terminal", report: { mode: "bad" } }),
    (error) => {
      assert.ok(error instanceof api.ApiRequestError);
      assert.equal(error.code, "INVALID_REPORT");
      assert.equal(error.status, 422);
      assert.deepEqual(error.details, { field: "mode" });
      return true;
    }
  );
  await api.storePolymarketLiveValidationReport({ label: "invalid-terminal", report: { mode: "bad" } });

  assert.notEqual(keys[0], keys[1]);
});

test("durable creates and live order management send generated idempotency keys", async () => {
  useUuidSequence(
    "00000000-0000-4000-8000-000000000011",
    "00000000-0000-4000-8000-000000000012",
    "00000000-0000-4000-8000-000000000013",
    "00000000-0000-4000-8000-000000000014"
  );
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return jsonResponse(200, { ok: true });
  };

  await api.createAlert({ token_id: "token-1", label: "Watch", direction: "above", threshold: 0.7 });
  await api.createWallet({ wallet: "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" });
  await api.submitPaperOrder({
    market_id: "kalshi",
    contract_id: "KXTEST:YES",
    side: "BUY",
    size: "2",
    limit_price: "0.4",
    metadata_json: ""
  });
  await api.manageMarketOrders("kalshi", "cancel_order", { order_id: "order-1" });

  assert.deepEqual(
    calls.map((call) => new URL(call.url).pathname),
    [
      "/api/alerts",
      "/api/wallets",
      "/api/paper/orders",
      "/api/markets/kalshi/orders/cancel_order"
    ]
  );
  assert.ok(calls.every((call) => call.options.method === "POST"));
  const keys = calls.map((call) => call.options.headers["Idempotency-Key"]);
  assert.equal(new Set(keys).size, 4);
  assert.ok(keys.every((key) => key.startsWith("market-sentinel-")));
});

test("ambiguous live order responses retain the same key for reconciliation", async () => {
  useUuidSequence("00000000-0000-4000-8000-000000000021");
  const keys = [];
  globalThis.fetch = async (_url, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    return jsonResponse(503, {
      error: {
        code: "live_mutation_reconciliation_required",
        message: "reconcile venue history",
        status: 503
      }
    });
  };

  const request = { order_id: "order-ambiguous" };
  await assert.rejects(api.manageMarketOrders("kalshi", "cancel_order", request), api.ApiRequestError);
  await assert.rejects(api.manageMarketOrders("kalshi", "cancel_order", request), api.ApiRequestError);

  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
});

test("paper metadata is parsed as an object before a live preflight request", async () => {
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse(200, { ok: true });
  };
  const form = {
    market_id: "polymarket",
    contract_id: "contract-1",
    side: "BUY",
    size: "2",
    limit_price: "0.4",
    metadata_json: '{"forecast":{"yes":0.61}}'
  };

  await api.previewLivePreflight(form);
  const body = JSON.parse(request.options.body);
  assert.equal(request.url, "http://127.0.0.1:8765/api/live-safety/preflight");
  assert.deepEqual(body.metadata, { forecast: { yes: 0.61 } });
  assert.equal("metadata_json" in body, false);

  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return jsonResponse(200, {});
  };
  assert.throws(() => api.previewLivePreflight({ ...form, metadata_json: "[]" }), /must be a JSON object/);
  assert.equal(fetchCalled, false);
});

test("schema diagnostics reject malformed values and normalize safe arrays", () => {
  assert.equal(api.apiSchemaValidation({ schema_validation: { ok: "yes" } }), null);
  assert.deepEqual(
    api.apiSchemaValidation({
      schema_validation: {
        schema_version: "2",
        ok: false,
        mode: "funded_audit",
        report_type: null,
        errors: ["bad mode", 42],
        warnings: ["review"],
        accepted_modes: ["dry_run"]
      }
    }),
    {
      schema_version: 2,
      ok: false,
      mode: "funded_audit",
      report_type: null,
      errors: ["bad mode", "42"],
      warnings: ["review"],
      accepted_modes: ["dry_run"]
    }
  );
});
