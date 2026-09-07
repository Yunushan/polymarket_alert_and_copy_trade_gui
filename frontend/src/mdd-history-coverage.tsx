import type { PolymarketMddPayload } from "./types.js";

function timestamp(value: number | null): string {
  const date = value === null ? null : new Date(value * 1000);
  return date && Number.isFinite(date.getTime()) ? date.toISOString() : "Unknown";
}

export function MddHistoryCoverage({ payload }: { payload: PolymarketMddPayload }) {
  const sources = Object.entries(payload.mdd_history_coverage ?? {});
  return (
    <div data-mdd-history={payload.mdd_history_status ?? "unknown"}>
      <p className="muted">Public history; account equity unverified</p>
      {payload.mdd_source_quality?.status === "invalid" ? (
        <div role="alert">
          <strong>Risk unavailable: invalid source data</strong>
          <ul>
            {Object.entries(payload.mdd_source_quality.sources).filter(([, source]) => source.invalid_rows > 0).map(([name, source]) => (
              <li key={name}>{name.replaceAll("_", " ")}: {source.invalid_rows} / {source.rows} invalid rows
                {" ("}{Object.entries(source.reasons).map(([reason, count]) => `${reason.replaceAll("_", " ")}: ${count}`).join(", ")}{")"}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="table-wrap">
      <table aria-label="Public history coverage">
        <thead>
          <tr><th>Source</th><th>Status</th><th>Rows / limit</th><th>First observed (UTC)</th><th>Last observed (UTC)</th></tr>
        </thead>
        <tbody>
          {sources.map(([name, source]) => (
            <tr key={name}>
              <td>{name.replaceAll("_", " ")}</td>
              <td>{source.status.replaceAll("_", " ")}</td>
              <td>{source.returned} / {source.limit}</td>
              <td>{timestamp(source.first_timestamp)}</td>
              <td>{timestamp(source.last_timestamp)}</td>
            </tr>
          ))}
          {!sources.length ? <tr><td colSpan={5}>History coverage unverified</td></tr> : null}
        </tbody>
      </table>
      </div>
    </div>
  );
}
