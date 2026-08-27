# Copy-activity dispatch reconciliation

Wallet copy signals are checkpointed together with a durable outbox entry. A
live order is marked `ambiguous` immediately before the venue call. This is a
deliberately conservative state: a timeout, process crash, or lost response may
mean that the venue accepted the order, so the application will not submit it
again automatically.

Shut down the application before reconciling. List unresolved entries with the same
configuration path used by the application:

```powershell
python scripts/reconcile_copy_activity.py --config data/config.json list
```

Compare the entry's contract, side, size, price, and time with the venue's order
and trade history. Then record exactly one explicit resolution:

```powershell
# Venue history proves the order was dispatched: close without retry.
python scripts/reconcile_copy_activity.py --config data/config.json resolve ENTRY_ID confirmed_dispatched

# Venue history proves no order was dispatched: open one short, controlled
# replay window while the bound policy still matches.
python scripts/reconcile_copy_activity.py --config data/config.json resolve ENTRY_ID confirmed_not_dispatched

# Intentionally close the signal without retrying it.
python scripts/reconcile_copy_activity.py --config data/config.json resolve ENTRY_ID discard
```

Never use `confirmed_not_dispatched` based only on a client-side timeout. Obtain
conclusive venue evidence first. This resolution preserves the original source
timestamp but records a fresh operator authorization for a five-minute replay
window. The bound market and execution policy must still match, and fresh
geoblock, executable-quote, preflight, and durable dispatch-intent checks run
again before any venue call. Use `discard` instead if replaying the old signal is
no longer appropriate. Configuration saves use revision checks, so the command
fails rather than overwriting a concurrently changed application state. Restart
the application after the command succeeds.
