import type { LivePreflightPayload } from "./types.js";
import { formatAuditValue, formatNumber } from "./formatting.js";

export function livePreflightBlocked(payload: Pick<LivePreflightPayload, "blocked" | "ok">): boolean {
  // A contradictory or incomplete response must never be presented as a pass.
  return payload.blocked || !payload.ok;
}

export function LivePreflightAudit({ payload }: { payload: LivePreflightPayload }) {
  const preflight = payload.preflight;
  const blocked = livePreflightBlocked(payload);
  const detail = String(payload.error || payload.message || "").trim();
  const redactionEnabled = payload.live_safety.redaction.audit_payloads_redacted === true;

  return (
    <div className="audit-box" data-preflight-result={blocked ? "blocked" : "passed"}>
      <div className="diagnostic-grid">
        <div>
          <span>Result</span>
          <strong>{blocked ? "blocked" : "passed"}</strong>
        </div>
        <div>
          <span>Market</span>
          <strong>{payload.order.market_id}</strong>
        </div>
        <div>
          <span>Contract</span>
          <strong>{payload.order.contract_id}</strong>
        </div>
        <div>
          <span>Order</span>
          <strong>
            {payload.order.side} {formatNumber(payload.order.size, 4)} @ {formatNumber(payload.order.limit_price, 4)}
          </strong>
        </div>
        <div>
          <span>Notional</span>
          <strong>{formatNumber(payload.order.approx_notional, 4)}</strong>
        </div>
        <div>
          <span>Gate</span>
          <strong>{payload.live_safety.status}</strong>
        </div>
        <div>
          <span>Metadata keys</span>
          <strong>{payload.order.metadata_keys.length ? payload.order.metadata_keys.join(", ") : "-"}</strong>
        </div>
        <div>
          <span>Redaction</span>
          <strong>{redactionEnabled ? "enabled" : "disabled"}</strong>
        </div>
      </div>
      {detail ? (
        <div className={`info-banner ${blocked ? "warn" : ""}`} role={blocked ? "alert" : "status"}>
          <strong>Preflight detail</strong>
          <span>{detail}</span>
        </div>
      ) : null}
      {preflight ? (
        <div className="diagnostic-grid">
          <div>
            <span>Adapter</span>
            <strong>{formatAuditValue(preflight.display_name)}</strong>
          </div>
          <div>
            <span>Feature</span>
            <strong>{formatAuditValue(preflight.feature)}</strong>
          </div>
          <div>
            <span>Max size</span>
            <strong>{formatAuditValue(preflight.max_size)}</strong>
          </div>
          <div>
            <span>Max notional</span>
            <strong>{formatAuditValue(preflight.max_notional)}</strong>
          </div>
          <div>
            <span>Warnings</span>
            <strong>{formatAuditValue(preflight.warnings)}</strong>
          </div>
          <div>
            <span>Credentials</span>
            <strong>{formatAuditValue(preflight.requires_credentials)}</strong>
          </div>
          <div>
            <span>KYC</span>
            <strong>{formatAuditValue(preflight.requires_kyc)}</strong>
          </div>
          <div>
            <span>Region limited</span>
            <strong>{formatAuditValue(preflight.region_limited)}</strong>
          </div>
        </div>
      ) : null}
    </div>
  );
}
