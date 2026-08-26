GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BRIDGE_API = "https://bridge.polymarket.com"
RELAYER_API = "https://relayer-v2.polymarket.com"

# Polymarket's CLOB V2 migration is not backward compatible with legacy
# py-clob-client/V1-signed mutation flows. The repository now contains an
# offline-tested V2 wrapper, but execution remains disabled until credentialed
# and funded order/cancel evidence is bound to the exact promoted revision.
POLYMARKET_CLOB_V2_MIGRATION_URL = "https://docs.polymarket.com/v2-migration"
POLYMARKET_CLOB_V2_CLIENT_IMPLEMENTED = True
POLYMARKET_LIVE_MUTATIONS_SUPPORTED = False
POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED = False
POLYMARKET_LIVE_MUTATION_BLOCKER = (
    "Polymarket CLOB V2 mutations are disabled until exact-revision credentialed "
    "and funded order/cancel verification is reviewed and promoted; legacy "
    "py-clob-client/V1 order flows must not be used in production."
)
POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER = (
    "The bounded Polymarket CLOB V2 funded order/cancel audit is disabled until an operator-approved "
    "durable recovery journal is wired to the exact clean revision; normal product mutation "
    "support and bounded audit mutation support are independent gates."
)

# WebSocket base (append /ws/market or /ws/user)
CLOB_WSS_BASE = "wss://ws-subscriptions-clob.polymarket.com"
SPORTS_WSS_BASE = "wss://sports-api.polymarket.com"
