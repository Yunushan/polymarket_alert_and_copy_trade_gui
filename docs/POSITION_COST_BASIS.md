# Public Position Cost Basis

MDD calculation version 7 corrects position units and open-position query scope.
It does not implement verified investment ROI or a full account-equity ledger.

## Sources

The [current Data API contract](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)
defines `grossInitialValue` as remaining cost including attributed BUY fees.
`initialValue` and `avgPrice` exclude those fees. Optional absent fields are
unavailable, whereas an explicit zero fee is known. The default query excludes
archived active positions and uses a size threshold of one.

The [official historical position schema](https://github.com/Polymarket/polymarket-subgraph/blob/7a92ba026a9466c07381e0d245a323ba23ee8701/pnl-subgraph/schema.graphql)
identifies `totalBought` as token quantity. The current closed-position endpoint
lists numeric `totalBought`/`avgPrice` fields without a detailed cost ledger.
Multiplying those fields yields a public share-price estimate, not independently
verified historical spending or starting account capital. The retired subgraph
is schema evidence only, not a supported current data source.

## Calculation

1. Prefer explicit `grossInitialValue`; do not add its fee component again.
2. Otherwise use `initialValue`, adding `entryFeesUsdc` only when supplied.
3. Otherwise multiply bought shares by average price for a closed position, or
   remaining `size` by average price for an open position. A reported fee is
   additive to this fee-exclusive estimate.
4. Without a cost or both price and quantity, report the position cost as
   unavailable. Neither raw share count nor current market value is entry cost.

Malformed, non-finite, negative or contradictory entry-cost components invalidate
risk even when an operator supplies a capital base. Cost-component comparisons
allow the source's six-decimal rounding. Missing fee components remain visible
in provenance; none of these fields establishes complete BUY/SELL fee history.
The source-reported PnL is not relabeled as a fully fee-reconciled net return.

`position_capital_basis` records the unit, selected sources and unknown-row
counts in MDD and leaderboard JSON/CSV and durable scan summaries. Old summaries
without this field do not gain invented provenance. Resuming a version-6 scan
invalidates its MDD enrichment while preserving downloaded leaderboard pages.

MDD requests `sizeThreshold=0` and `includeArchived=true` on every open-position
page. Query filters are recorded in `mdd_history_coverage`. General callers of
`get_positions` retain the documented defaults unless they opt in. Page/row
caps, source-quality checks and unverified-account labels remain in force.

The automatic public denominator still uses aggregate position/trade estimates;
capital reuse can differ from this basis. Full historical cash flows, inventory,
valuations, fees and independent portfolio reconciliation remain required before
the account-level MDD <=20% requirement can be certified.
