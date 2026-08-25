from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_TRUEO_RPC_URL = "https://mainnet.base.org"
DEFAULT_TRUEO_MANAGER = "0x61A98Bef11867c69489B91f340fE545eEfc695d7"
DEFAULT_TRUEO_V4_POOL_MANAGER = "0x498581fF718922c3f8e6A244956aF099B2652b2b"
DEFAULT_TRUEO_V4_STATE_VIEW = "0xA3c0c9b65baD0b08107Aa264b0f3dB444b867A71"
DEFAULT_TRUEO_CHAIN_ID = 8453
TRUEO_REFERENCES = (
    "https://docs.trueo.com/deployments",
    "https://docs.trueo.com/markets",
    "https://docs.trueo.com/trading",
    "https://github.com/trueo-protocol/trueo-contracts",
)
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Keccak-256 selectors from the official TruthMarketManager, TruthMarket and
# Uniswap V3 pool interfaces. Keeping the selectors explicit avoids shipping a
# generated ABI or a wallet library just to perform read-only calls.
SELECTORS = {
    "active_count": "7d6a0d1a",
    "active_market": "dd5adfa3",
    "is_active_market": "6ec38a4e",
    "version": "ffa1ad74",
    "question": "066f69af",
    "source": "17447836",
    "additional_info": "4063c865",
    "end_of_trading": "d6a05e67",
    "status": "a3dd2619",
    "winning_position": "2486d671",
    "yes_token": "f0d9bb20",
    "no_token": "11a9f10a",
    "payment_token": "3013ce29",
    "pools": "e4b6db4c",
    "pool_ids": "b4f2bb6d",
    "pool_keys": "d183feee",
    "hook_address": "32a3cf96",
    "slot0": "3850c7bd",
    "v4_slot0": "c815641c",
    "pool_manager": "dc4c90d3",
    "token0": "0dfe1681",
    "token1": "d21220a7",
    "decimals": "313ce567",
}

UNISWAP_V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
UNISWAP_V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
DEFAULT_TRUEO_LOG_WINDOW_BLOCKS = 10_000
MAX_TRUEO_LOG_WINDOW_BLOCKS = 500_000
DEFAULT_TRUEO_LOG_QUERY_BLOCKS = 10_000
MAX_TRUEO_LOG_QUERY_BLOCKS = 10_000
DEFAULT_TRUEO_BATCH_CALL_SIZE = 5
MAX_TRUEO_BATCH_CALL_SIZE = 10
DEFAULT_TRUEO_EVENT_SCAN_LIMIT = 200
MAX_TRUEO_EVENT_SCAN_LIMIT = 2_000
DEFAULT_TRUEO_RPC_MAX_RETRIES = 3
MAX_TRUEO_RPC_MAX_RETRIES = 5
DEFAULT_TRUEO_RPC_RETRY_BACKOFF_SECONDS = 1.0
MAX_TRUEO_RPC_RETRY_BACKOFF_SECONDS = 10.0
DEFAULT_TRUEO_CONFIRMATION_BLOCKS = 12
MAX_TRUEO_CONFIRMATION_BLOCKS = 10_000
DEFAULT_TRUEO_MAX_TRADE_LOGS = 500
MAX_TRUEO_MAX_TRADE_LOGS = 5_000
DEFAULT_TRUEO_MAX_BLOCK_HEADERS = 200
MAX_TRUEO_MAX_BLOCK_HEADERS = 500
DEFAULT_TRUEO_MAX_RPC_RESPONSE_BYTES = 2_000_000
MAX_TRUEO_MAX_RPC_RESPONSE_BYTES = 10_000_000


class TrueoAdapter(MarketAdapter):
    """Official Trueo Base on-chain adapter.

    Trueo publishes no hosted market-data API: the supported integration is the
    deployed ``TruthMarketManager`` and each market's immutable on-chain fields.
    This adapter reads the manager/market contracts through JSON-RPC, derives a
    current YES/NO AMM price from the documented Uniswap V3 or V4 pools, and
    keeps paper trading local. Live execution accepts only a complete,
    externally signed raw transaction and remains disabled unless the operator
    enables the explicit submission gate.
    """

    metadata = get_market_metadata("trueo")
    live_order_sides = ("BUY", "SELL")

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._market_cache: Dict[str, Dict[str, Any]] = {}
        self._token_decimals_cache: Dict[str, int] = {}

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("trueo_rpc_url") or self.config.get("evm_rpc_url")
        value = str(configured or DEFAULT_TRUEO_RPC_URL).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Trueo RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def manager_address(self) -> str:
        value = self.config.get("trueo_manager_address") or DEFAULT_TRUEO_MANAGER
        return self._address(value, label="manager address")

    @property
    def v4_pool_manager_address(self) -> str:
        value = self.config.get("trueo_v4_pool_manager_address") or DEFAULT_TRUEO_V4_POOL_MANAGER
        return self._address(value, label="V4 pool manager address")

    @property
    def v4_state_view_address(self) -> str:
        value = self.config.get("trueo_v4_state_view_address") or DEFAULT_TRUEO_V4_STATE_VIEW
        return self._address(value, label="V4 StateView address")

    @property
    def chain_id(self) -> int:
        value = self.config.get("trueo_chain_id", DEFAULT_TRUEO_CHAIN_ID)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo chain ID must be a positive integer.")
        try:
            chain_id = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo chain ID must be a positive integer.") from exc
        if chain_id <= 0:
            raise MarketConfigurationError("Trueo chain ID must be a positive integer.")
        return chain_id

    @property
    def live_transaction_targets(self) -> Tuple[str, ...]:
        configured = self.config.get("trueo_live_transaction_targets")
        if configured in (None, ""):
            return ()
        values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
        addresses: List[str] = []
        for value in values:
            address = self._address(value, label="live transaction target")
            if address.casefold() not in {item.casefold() for item in addresses}:
                addresses.append(address)
        return tuple(addresses)

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "rpc_url": self.rpc_url,
                "manager_address": self.manager_address,
                "v4_pool_manager_address": self.v4_pool_manager_address,
                "v4_state_view_address": self.v4_state_view_address,
                "chain_id": self.chain_id,
                "network": "Base mainnet",
                "references": list(TRUEO_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "allowlisted_live_transaction_target_count": len(self.live_transaction_targets),
                "wallet_transaction_required": True,
                "settlement_required": True,
                "trade_history_source": "uniswap_v3_and_v4_swap_logs",
                "trade_history_bounded": True,
                "log_window_blocks": self.log_window_blocks,
                "log_query_blocks": self.log_query_blocks,
                "max_trade_logs": self.max_trade_logs,
                "max_block_headers": self.max_block_headers,
                "max_rpc_response_bytes": self.max_rpc_response_bytes,
                "batch_call_size": self.batch_call_size,
                "event_query_scan_limit": self.event_scan_limit,
                "event_inventory_order": "newest_first",
                "history_confirmation_blocks": self.confirmation_blocks,
                "rpc_max_retries": self.rpc_max_retries,
                "rpc_retry_backoff_seconds": self.rpc_retry_backoff_seconds,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        count = self._call_uint(self.manager_address, SELECTORS["active_count"])
        if count <= 0:
            return []
        needle = str(query or "").strip().lower()
        self._market_cache = {}
        scan_count = min(count, desired if not needle else self.event_scan_limit)
        indices = list(range(count - 1, count - scan_count - 1, -1))
        address_values = self._batch_eth_calls(
            [
                (
                    f"market:{index}",
                    self.manager_address,
                    SELECTORS["active_market"] + self._uint_arg(index),
                )
                for index in indices
            ]
        )
        addresses = []
        for index in indices:
            value = address_values[f"market:{index}"]
            if value is None:
                raise MarketHTTPError("Trueo manager failed to return an active market address.")
            addresses.append(self._call_result_address(value))
        summaries = self._read_market_summaries(addresses)
        rows: List[MarketEvent] = []
        for index, address in zip(indices, addresses, strict=True):
            row = summaries[address.casefold()]
            title = str(row["question"] or address)
            if needle and needle not in title.lower() and needle not in str(row["source"]).lower():
                continue
            raw = {
                **row,
                "inventory_index": index,
                "inventory_count": count,
                "inventory_scan_count": scan_count,
                "inventory_scan_truncated": scan_count < count,
            }
            rows.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=address,
                    title=title,
                    url=f"https://basescan.org/address/{address}",
                    status=str(row["status_name"]),
                    raw=raw,
                )
            )
            if len(rows) >= desired:
                break
        if needle and len(rows) < desired and scan_count < count:
            raise MarketHTTPError(
                "Trueo query results are incomplete because the bounded event scan did not cover the full "
                f"inventory ({scan_count} of {count} markets). Increase trueo_event_scan_limit or request a "
                "known market address directly."
            )
        return rows

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        address = self._address(event_id, label="market address")
        row = self._market_cache.get(address.lower()) or self._read_market(address)
        title = str(row.get("question") or address)
        status = str(row.get("status_name") or "unknown")
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=f"{address}:0",
                event_id=address,
                title=f"{title} - YES",
                outcome="YES",
                url=f"https://basescan.org/address/{address}",
                status=status,
                raw={"market": dict(row), "outcome": 1, "token": row["yes_token"]},
            ),
            MarketContract(
                market_id=self.market_id,
                contract_id=f"{address}:1",
                event_id=address,
                title=f"{title} - NO",
                outcome="NO",
                url=f"https://basescan.org/address/{address}",
                status=status,
                raw={"market": dict(row), "outcome": 2, "token": row["no_token"]},
            ),
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_address, outcome_index = self._split_contract_id(contract_id)
        row = self._market_cache.get(market_address.lower()) or self._read_market(market_address)
        payment_token = row["payment_token"]
        outcome_token = row["yes_token"] if outcome_index == 0 else row["no_token"]
        amm_version = str(row["amm_version"])
        if amm_version == "v4":
            pool = row["yes_pool_id"] if outcome_index == 0 else row["no_pool_id"]
            value, raw = self._v4_pool_price(pool, outcome_token, payment_token)
            source = "trueo_uniswap_v4_state_view"
        else:
            pool = row["yes_pool"] if outcome_index == 0 else row["no_pool"]
            value, raw = self._pool_price(pool, outcome_token, payment_token)
            source = "trueo_uniswap_v3_slot0"
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=f"{market_address}:{outcome_index}",
            last=value,
            midpoint=value,
            source=source,
            raw={"market": dict(row), "pool": pool, "pool_read": raw},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Trueo documents Uniswap liquidity pools rather than a CLOB; slot0-derived AMM prices are not an orderbook.",
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return bounded public Uniswap V3/V4 swaps for one Trueo outcome pool.

        Trueo routes outcome trades through the Uniswap V3 or V4 pools created
        by its official market contracts. The two Swap events use opposite
        delta perspectives (V3 pool balances, V4 swap caller), which are
        normalized here into outcome-side BUY/SELL, size, and collateral price
        without pretending that a router ``sender`` is the end-user wallet.
        The block range and result count are deliberately bounded.
        """

        self.ensure_capability("trade_history")
        market_address, outcome_index = self._split_contract_id(contract_id)
        row = self._market_cache.get(market_address.lower()) or self._read_market(market_address)
        outcome_token = row["yes_token"] if outcome_index == 0 else row["no_token"]
        payment_token = row["payment_token"]
        desired = self._trade_limit(limit)
        after_ts = self._history_timestamp(after, "after") if after is not None else 0.0
        before_ts = self._history_timestamp(before, "before") if before is not None else 253_402_300_799.0
        if before_ts < after_ts:
            raise MarketConfigurationError("Trueo trade history requires before to be at or after after.")

        from_block, to_block = self._trade_log_block_bounds()
        history_coverage = self._history_coverage(
            from_block,
            to_block,
            after=after_ts if after is not None else None,
            before=before_ts if before is not None else None,
        )
        amm_version = str(row["amm_version"])
        if amm_version == "v4":
            pool = row["yes_pool_id"] if outcome_index == 0 else row["no_pool_id"]
            log_address = self.v4_pool_manager_address
            topics = [UNISWAP_V4_SWAP_TOPIC, pool]
            token0, token1 = self._sorted_token_pair(outcome_token, payment_token)
            source = "trueo_uniswap_v4_swap"
        else:
            pool = row["yes_pool"] if outcome_index == 0 else row["no_pool"]
            log_address = pool
            topics = [UNISWAP_V3_SWAP_TOPIC]
            token0 = self._call_address(pool, SELECTORS["token0"])
            token1 = self._call_address(pool, SELECTORS["token1"])
            source = "trueo_uniswap_v3_swap"
        logs = self._fetch_swap_logs(log_address, topics, from_block, to_block)
        decimals0 = self._token_decimals(token0)
        decimals1 = self._token_decimals(token1)
        if {token0.lower(), token1.lower()} != {outcome_token.lower(), payment_token.lower()}:
            raise MarketConfigurationError("Trueo swap pool tokens do not match the market outcome/payment tokens.")

        canonical = f"{market_address}:{outcome_index}"
        candidates: List[Dict[str, Any]] = []
        seen_trade_ids: set[str] = set()
        for log in logs:
            decoded = (
                self._decode_v4_swap_log(log, pool)
                if amm_version == "v4"
                else self._decode_swap_log(log, pool)
            )
            if decoded is None:
                continue
            if not from_block <= decoded["block_number"] <= to_block:
                continue
            trade_id = f"{decoded['transaction_hash']}:{decoded['log_index']}"
            if trade_id.casefold() in seen_trade_ids:
                continue
            seen_trade_ids.add(trade_id.casefold())
            outcome_delta = decoded["amount0"] if token0.lower() == outcome_token.lower() else decoded["amount1"]
            payment_delta = decoded["amount1"] if token0.lower() == outcome_token.lower() else decoded["amount0"]
            if outcome_delta == 0 or payment_delta == 0:
                continue
            scale_outcome = 10**decimals0 if token0.lower() == outcome_token.lower() else 10**decimals1
            scale_payment = 10**decimals1 if token0.lower() == outcome_token.lower() else 10**decimals0
            size = abs(outcome_delta) / float(scale_outcome)
            payment = abs(payment_delta) / float(scale_payment)
            price = payment / size if size else 0.0
            if not math.isfinite(size) or not math.isfinite(price) or size <= 0 or price <= 0 or price > 1:
                continue
            received_outcome = outcome_delta > 0 if amm_version == "v4" else outcome_delta < 0
            candidates.append(
                {
                    "trade_id": trade_id,
                    "side": "BUY" if received_outcome else "SELL",
                    "price": price,
                    "size": size,
                    "raw": {
                        "source": source,
                        "amm_version": amm_version,
                        "pool": pool,
                        "market_address": market_address,
                        "outcome_index": outcome_index,
                        "sender": decoded["sender"],
                        "recipient": decoded.get("recipient"),
                        "amount0": decoded["amount0"],
                        "amount1": decoded["amount1"],
                        "sqrt_price_x96": decoded["sqrt_price_x96"],
                        "liquidity": decoded["liquidity"],
                        "tick": decoded["tick"],
                        "fee": decoded.get("fee"),
                        "block_number": decoded["block_number"],
                        "transaction_hash": decoded["transaction_hash"],
                        "log_index": decoded["log_index"],
                        "token0": token0,
                        "token1": token1,
                        "outcome_token": outcome_token,
                        "payment_token": payment_token,
                        "history_coverage": dict(history_coverage),
                    },
                }
            )
        candidates.sort(
            key=lambda candidate: (
                int(candidate["raw"]["block_number"]),
                int(candidate["raw"]["log_index"]),
                str(candidate["raw"]["transaction_hash"]),
            ),
            reverse=True,
        )
        if after is None and before is None:
            candidates = candidates[:desired]
        block_timestamps = self._batch_block_timestamps(
            {int(candidate["raw"]["block_number"]) for candidate in candidates}
        )
        trades: List[MarketTrade] = []
        for candidate in candidates:
            timestamp = block_timestamps[int(candidate["raw"]["block_number"])]
            if timestamp < after_ts or timestamp > before_ts:
                continue
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=str(candidate["trade_id"]),
                    side=str(candidate["side"]),
                    price=float(candidate["price"]),
                    size=float(candidate["size"]),
                    timestamp=timestamp,
                    raw=dict(candidate["raw"]),
                )
            )
        return trades[:desired]

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive bounded OHLCV candles from the public swap event tape."""

        self.ensure_capability("candle_history")
        resolution_key = str(resolution or "1h").strip().lower()
        intervals = {"1m": 60, "5m": 300, "15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400}
        if resolution_key not in intervals:
            raise MarketConfigurationError(
                "Trueo candle history accepts resolution 1m, 5m, 15m, 1h, 4h, or 1d."
            )
        lower = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        upper = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if lower is not None and upper is not None and upper < lower:
            raise MarketConfigurationError("Trueo candle history requires from_timestamp <= to_timestamp.")

        market_address, outcome_index = self._split_contract_id(contract_id)
        canonical = f"{market_address}:{outcome_index}"
        trades = self.list_trades(
            contract_id,
            limit=self.max_trade_logs,
            after=lower,
            before=upper,
        )
        buckets: Dict[int, List[MarketTrade]] = {}
        interval = intervals[resolution_key]
        for trade in trades:
            if trade.timestamp is None:
                continue
            bucket = int(float(trade.timestamp) // interval) * interval
            buckets.setdefault(bucket, []).append(trade)

        candles: List[MarketCandle] = []
        bucket_timestamps = sorted(buckets)
        for timestamp in bucket_timestamps:
            bucket_trades = buckets[timestamp]
            ordered = sorted(bucket_trades, key=self._trade_chain_order)
            prices = [float(trade.price) for trade in ordered]
            partial_reasons: List[str] = []
            if timestamp == bucket_timestamps[0]:
                partial_reasons.append("bounded_history_start")
            if timestamp == bucket_timestamps[-1]:
                partial_reasons.append("bounded_history_end")
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=float(timestamp),
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=sum(float(trade.size) for trade in ordered),
                    raw={
                        "source": "trueo_uniswap_swap",
                        "resolution": resolution_key,
                        "trade_count": len(ordered),
                        "trade_ids": [trade.trade_id for trade in ordered],
                        "history_coverage": dict(ordered[0].raw.get("history_coverage") or {}),
                        "partial": bool(partial_reasons),
                        "partial_reasons": partial_reasons,
                    },
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        market_address, outcome_index = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Trueo order side must be BUY or SELL.")
        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError("Trueo order size must be positive.")
        price = None
        if order.limit_price is not None:
            price = self._finite_float(order.limit_price, "limit price")
            if price <= 0 or price > 1:
                raise MarketConfigurationError("Trueo limit price must be greater than 0 and at most 1.")
        if price is None:
            price = self.get_price(f"{market_address}:{outcome_index}").last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=f"{market_address}:{outcome_index}",
            accepted=True,
            message=f"DRY RUN: would place Trueo {side} for {size:.6f} outcome units at {price:.6f}",
            average_price=price,
            raw={
                "dry_run": True,
                "request": {
                    "market_address": market_address,
                    "outcome_index": outcome_index,
                    "side": side,
                    "size": size,
                    "limit_price": price,
                    "execution_model": "Uniswap V3 market pool",
                },
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_order_market(order)
        market_address, outcome_index = self._split_contract_id(order.contract_id)
        audit = self.preflight_live_order(order, feature_name="Trueo live trading")
        if not self.config_bool("trueo_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "Trueo live trading requires trueo_submit_signed_transactions=true after reviewing the signed transaction."
            )
        allowlisted_targets = self.live_transaction_targets
        if not allowlisted_targets:
            raise MarketConfigurationError(
                "Trueo live orders require at least one explicitly reviewed trueo_live_transaction_targets entry."
            )

        metadata = dict(order.metadata or {})
        signed = metadata.get("signed_transaction")
        decoded = self._decode_signed_transaction(signed)

        reviewed_chain_id = self._metadata_int(metadata, "chain_id", label="reviewed chain ID")
        if reviewed_chain_id != self.chain_id or decoded["chain_id"] != self.chain_id:
            raise MarketConfigurationError("Trueo signed transaction targets a different chain than the configured network.")

        reviewed_target = self._address(metadata.get("transaction_to"), label="reviewed transaction target")
        decoded_target = str(decoded["to"])
        if reviewed_target.casefold() != decoded_target.casefold():
            raise MarketConfigurationError("Trueo signed transaction recipient does not match reviewed transaction metadata.")
        if not any(decoded_target.casefold() == target.casefold() for target in allowlisted_targets):
            raise MarketConfigurationError("Trueo signed transaction recipient is outside the reviewed target allow-list.")

        reviewed_data = self._calldata(metadata.get("transaction_data"), label="reviewed transaction calldata")
        if reviewed_data.casefold() != str(decoded["data"]).casefold():
            raise MarketConfigurationError("Trueo signed transaction calldata does not match reviewed transaction metadata.")

        reviewed_value = self._metadata_int(metadata, "transaction_value", label="reviewed transaction value", minimum=0)
        if reviewed_value != decoded["value"]:
            raise MarketConfigurationError("Trueo signed transaction value does not match reviewed transaction metadata.")

        reviewed_market = self._address(metadata.get("market_address"), label="reviewed market address")
        if reviewed_market.casefold() != market_address.casefold():
            raise MarketConfigurationError("Trueo signed transaction metadata targets a different market.")
        reviewed_outcome = self._metadata_int(metadata, "outcome_index", label="reviewed outcome index", minimum=0)
        if reviewed_outcome != outcome_index:
            raise MarketConfigurationError("Trueo signed transaction metadata targets a different outcome.")

        reviewed_side = str(metadata.get("side") or "").strip().upper()
        if reviewed_side != str(order.side or "").upper():
            raise MarketConfigurationError("Trueo signed transaction metadata uses a different order side.")
        self._validate_reviewed_number(metadata, "size", order.size, label="order size")
        self._validate_reviewed_limit_price(metadata, order.limit_price)

        response = self._rpc("eth_sendRawTransaction", [signed])
        if not isinstance(response, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", response):
            raise MarketHTTPError("Trueo RPC did not return a transaction hash.")
        return {
            "live": True,
            "tx_hash": response,
            "audit": audit,
            "signed_transaction_submitted": True,
            "chain_id": decoded["chain_id"],
            "transaction_to": decoded_target,
            "transaction_value": decoded["value"],
            "transaction_type": decoded["type"],
            "calldata_selector": reviewed_data[:10],
            "market_address": market_address,
            "outcome_index": outcome_index,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Trueo has no official account-activity mirroring API; copy trading is unsupported.",
        )

    @property
    def log_window_blocks(self) -> int:
        value = self.config.get("trueo_log_window_blocks", DEFAULT_TRUEO_LOG_WINDOW_BLOCKS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo log window must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo log window must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_LOG_WINDOW_BLOCKS:
            raise MarketConfigurationError(
                f"Trueo log window must be between 1 and {MAX_TRUEO_LOG_WINDOW_BLOCKS} blocks."
            )
        return parsed

    @property
    def log_query_blocks(self) -> int:
        value = self.config.get("trueo_log_query_blocks", DEFAULT_TRUEO_LOG_QUERY_BLOCKS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo log query size must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo log query size must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_LOG_QUERY_BLOCKS:
            raise MarketConfigurationError(
                f"Trueo log query size must be between 1 and {MAX_TRUEO_LOG_QUERY_BLOCKS} blocks."
            )
        return parsed

    @property
    def max_trade_logs(self) -> int:
        value = self.config.get("trueo_max_trade_logs", DEFAULT_TRUEO_MAX_TRADE_LOGS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo max trade logs must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo max trade logs must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_MAX_TRADE_LOGS:
            raise MarketConfigurationError(
                f"Trueo max trade logs must be between 1 and {MAX_TRUEO_MAX_TRADE_LOGS}."
            )
        return parsed

    @property
    def max_block_headers(self) -> int:
        value = self.config.get("trueo_max_block_headers", DEFAULT_TRUEO_MAX_BLOCK_HEADERS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo max block headers must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo max block headers must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_MAX_BLOCK_HEADERS:
            raise MarketConfigurationError(
                f"Trueo max block headers must be between 1 and {MAX_TRUEO_MAX_BLOCK_HEADERS}."
            )
        return parsed

    @property
    def max_rpc_response_bytes(self) -> int:
        value = self.config.get("trueo_max_rpc_response_bytes", DEFAULT_TRUEO_MAX_RPC_RESPONSE_BYTES)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo max RPC response bytes must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo max RPC response bytes must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_MAX_RPC_RESPONSE_BYTES:
            raise MarketConfigurationError(
                f"Trueo max RPC response bytes must be between 1 and {MAX_TRUEO_MAX_RPC_RESPONSE_BYTES}."
            )
        return parsed

    @property
    def batch_call_size(self) -> int:
        value = self.config.get("trueo_batch_call_size", DEFAULT_TRUEO_BATCH_CALL_SIZE)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo batch call size must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo batch call size must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_BATCH_CALL_SIZE:
            raise MarketConfigurationError(
                f"Trueo batch call size must be between 1 and {MAX_TRUEO_BATCH_CALL_SIZE}."
            )
        return parsed

    @property
    def event_scan_limit(self) -> int:
        value = self.config.get("trueo_event_scan_limit", DEFAULT_TRUEO_EVENT_SCAN_LIMIT)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo event scan limit must be a positive integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo event scan limit must be a positive integer.") from exc
        if parsed <= 0 or parsed > MAX_TRUEO_EVENT_SCAN_LIMIT:
            raise MarketConfigurationError(
                f"Trueo event scan limit must be between 1 and {MAX_TRUEO_EVENT_SCAN_LIMIT}."
            )
        return parsed

    @property
    def confirmation_blocks(self) -> int:
        value = self.config.get("trueo_confirmation_blocks", DEFAULT_TRUEO_CONFIRMATION_BLOCKS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo confirmation depth must be a non-negative integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo confirmation depth must be a non-negative integer.") from exc
        if parsed < 0 or parsed > MAX_TRUEO_CONFIRMATION_BLOCKS:
            raise MarketConfigurationError(
                f"Trueo confirmation depth must be between 0 and {MAX_TRUEO_CONFIRMATION_BLOCKS} blocks."
            )
        return parsed

    @property
    def rpc_max_retries(self) -> int:
        value = self.config.get("trueo_rpc_max_retries", DEFAULT_TRUEO_RPC_MAX_RETRIES)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo RPC retry count must be a non-negative integer.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo RPC retry count must be a non-negative integer.") from exc
        if parsed < 0 or parsed > MAX_TRUEO_RPC_MAX_RETRIES:
            raise MarketConfigurationError(
                f"Trueo RPC retry count must be between 0 and {MAX_TRUEO_RPC_MAX_RETRIES}."
            )
        return parsed

    @property
    def rpc_retry_backoff_seconds(self) -> float:
        value = self.config.get("trueo_rpc_retry_backoff_seconds", DEFAULT_TRUEO_RPC_RETRY_BACKOFF_SECONDS)
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo RPC retry backoff must be a non-negative number.")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo RPC retry backoff must be a non-negative number.") from exc
        if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_TRUEO_RPC_RETRY_BACKOFF_SECONDS:
            raise MarketConfigurationError(
                f"Trueo RPC retry backoff must be between 0 and {MAX_TRUEO_RPC_RETRY_BACKOFF_SECONDS} seconds."
            )
        return parsed

    def _trade_log_block_bounds(self) -> Tuple[int, int]:
        configured_to = self.config.get("trueo_log_to_block")
        if configured_to not in (None, ""):
            to_block = self._block_number(configured_to, label="trueo_log_to_block")
        else:
            latest_block = self._block_number(self._rpc("eth_blockNumber", []), label="latest block")
            to_block = max(0, latest_block - self.confirmation_blocks)
        configured_from = self.config.get("trueo_log_from_block")
        from_block = (
            self._block_number(configured_from, label="trueo_log_from_block")
            if configured_from not in (None, "")
            else max(0, to_block - self.log_window_blocks + 1)
        )
        if from_block > to_block:
            raise MarketConfigurationError("Trueo log range requires from_block <= to_block.")
        if to_block - from_block + 1 > MAX_TRUEO_LOG_WINDOW_BLOCKS:
            raise MarketConfigurationError(
                f"Trueo log range may span at most {MAX_TRUEO_LOG_WINDOW_BLOCKS} blocks."
            )
        return from_block, to_block

    def _history_coverage(
        self,
        from_block: int,
        to_block: int,
        *,
        after: Optional[float],
        before: Optional[float],
    ) -> Dict[str, Any]:
        """Describe bounded history and reject timestamp requests outside it."""

        configured_from = self.config.get("trueo_log_from_block") not in (None, "")
        configured_to = self.config.get("trueo_log_to_block") not in (None, "")
        coverage: Dict[str, Any] = {
            "scope": "bounded_block_range",
            "from_block": from_block,
            "to_block": to_block,
            "scan_truncated_before": not configured_from and from_block > 0,
            "head_selection": "configured" if configured_to else "latest_minus_confirmations",
            "confirmation_blocks": None if configured_to else self.confirmation_blocks,
            "reorg_provisional": not configured_to and self.confirmation_blocks == 0,
        }
        if after is None and before is None:
            return coverage

        timestamps = self._batch_block_timestamps({from_block, to_block})
        start_timestamp = timestamps[from_block]
        end_timestamp = timestamps[to_block]
        coverage["from_timestamp"] = start_timestamp
        coverage["to_timestamp"] = end_timestamp
        if after is not None and after < start_timestamp:
            raise MarketConfigurationError(
                "Trueo requested history starts before the configured block coverage; widen "
                "trueo_log_from_block/trueo_log_window_blocks instead of accepting an incomplete result."
            )
        if before is not None and before > end_timestamp:
            raise MarketConfigurationError(
                "Trueo requested history ends after the confirmed/configured block coverage; choose an in-range "
                "timestamp or explicitly review the log head settings."
            )
        return coverage

    def _fetch_swap_logs(
        self,
        address: str,
        topics: List[str],
        from_block: int,
        to_block: int,
    ) -> List[Any]:
        """Fetch a bounded range in provider-friendly chunks."""

        rows: List[Any] = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + self.log_query_blocks - 1)
            payload = self._rpc(
                "eth_getLogs",
                [
                    {
                        "address": address,
                        "fromBlock": hex(start),
                        "toBlock": hex(end),
                        "topics": list(topics),
                    }
                ],
                max_response_bytes=self.max_rpc_response_bytes,
            )
            if not isinstance(payload, list):
                raise MarketHTTPError("Trueo eth_getLogs did not return a list.")
            rows.extend(payload)
            if len(rows) > self.max_trade_logs:
                raise MarketHTTPError(
                    f"Trueo RPC returned more than the configured safety limit of {self.max_trade_logs} swap logs."
                )
            start = end + 1
        return rows

    def _decode_swap_log(self, log: Any, pool: str) -> Optional[Dict[str, Any]]:
        if not isinstance(log, Mapping):
            return None
        address = self._address(log.get("address"), label="swap log address")
        if address.casefold() != pool.casefold():
            return None
        topics = log.get("topics")
        if bool(log.get("removed")):
            return None
        if not isinstance(topics, list) or len(topics) != 3:
            return None
        if str(topics[0]).casefold() != UNISWAP_V3_SWAP_TOPIC.casefold():
            return None
        sender = self._topic_address(topics[1], label="swap sender")
        recipient = self._topic_address(topics[2], label="swap recipient")
        data = log.get("data")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{320}", data):
            return None
        try:
            amount0, amount1, sqrt_price, liquidity, tick = self._decode(
                data,
                ("int256", "int256", "uint160", "uint128", "int24"),
            )
            block_number = self._block_number(log.get("blockNumber"), label="swap log block number")
            log_index = self._block_number(log.get("logIndex"), label="swap log index")
        except (MarketConfigurationError, TypeError, ValueError, OverflowError):
            return None
        transaction_hash = str(log.get("transactionHash") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
            return None
        if int(amount0) == 0 or int(amount1) == 0 or (int(amount0) > 0) == (int(amount1) > 0):
            return None
        if int(sqrt_price) <= 0 or int(liquidity) <= 0:
            return None
        return {
            "sender": sender,
            "recipient": recipient,
            "amount0": int(amount0),
            "amount1": int(amount1),
            "sqrt_price_x96": int(sqrt_price),
            "liquidity": int(liquidity),
            "tick": int(tick),
            "block_number": block_number,
            "log_index": log_index,
            "transaction_hash": transaction_hash,
        }

    def _decode_v4_swap_log(self, log: Any, pool_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(log, Mapping):
            return None
        try:
            address = self._address(log.get("address"), label="V4 swap log address")
        except MarketConfigurationError:
            return None
        if address.casefold() != self.v4_pool_manager_address.casefold() or bool(log.get("removed")):
            return None
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) != 3:
            return None
        if str(topics[0]).casefold() != UNISWAP_V4_SWAP_TOPIC.casefold():
            return None
        if str(topics[1]).casefold() != pool_id.casefold():
            return None
        try:
            sender = self._topic_address(topics[2], label="V4 swap sender")
        except MarketConfigurationError:
            return None
        data = log.get("data")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{384}", data):
            return None
        try:
            amount0, amount1, sqrt_price, liquidity, tick, fee = self._decode(
                data,
                ("int128", "int128", "uint160", "uint128", "int24", "uint24"),
            )
            block_number = self._block_number(log.get("blockNumber"), label="V4 swap log block number")
            log_index = self._block_number(log.get("logIndex"), label="V4 swap log index")
        except (MarketConfigurationError, TypeError, ValueError, OverflowError):
            return None
        transaction_hash = str(log.get("transactionHash") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
            return None
        if int(amount0) == 0 or int(amount1) == 0 or (int(amount0) > 0) == (int(amount1) > 0):
            return None
        if int(sqrt_price) <= 0 or int(liquidity) <= 0 or int(fee) < 0:
            return None
        return {
            "sender": sender,
            "amount0": int(amount0),
            "amount1": int(amount1),
            "sqrt_price_x96": int(sqrt_price),
            "liquidity": int(liquidity),
            "tick": int(tick),
            "fee": int(fee),
            "block_number": block_number,
            "log_index": log_index,
            "transaction_hash": transaction_hash,
        }

    @staticmethod
    def _trade_chain_order(trade: MarketTrade) -> Tuple[int, int, str]:
        raw = trade.raw if isinstance(trade.raw, Mapping) else {}
        return (
            int(raw.get("block_number") or 0),
            int(raw.get("log_index") or 0),
            str(raw.get("transaction_hash") or trade.trade_id),
        )

    def _block_timestamp(self, block_number: int) -> float:
        payload = self._rpc("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Trueo RPC did not return a block for a swap log.")
        timestamp = self._block_number(payload.get("timestamp"), label="block timestamp")
        if timestamp <= 0 or timestamp > 253_402_300_799:
            raise MarketHTTPError("Trueo swap block timestamp is outside the supported range.")
        return float(timestamp)

    def _batch_block_timestamps(self, block_numbers: set[int]) -> Dict[int, float]:
        ordered = sorted(block_numbers)
        if len(ordered) > self.max_block_headers:
            raise MarketHTTPError(
                f"Trueo history requires {len(ordered)} block headers, exceeding the configured safety limit "
                f"of {self.max_block_headers}."
            )
        requests = [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "eth_getBlockByNumber",
                "params": [hex(block_number), False],
            }
            for request_id, block_number in enumerate(ordered, start=1)
        ]
        payloads = self._batch_rpc_requests(requests)
        timestamps: Dict[int, float] = {}
        for request_id, block_number in enumerate(ordered, start=1):
            payload = payloads.get(request_id)
            if not isinstance(payload, Mapping):
                raise MarketHTTPError("Trueo RPC did not return a block for a swap log.")
            returned_number = self._block_number(payload.get("number"), label="block header number")
            if returned_number != block_number:
                raise MarketHTTPError("Trueo RPC returned a mismatched block header.")
            timestamp = self._block_number(payload.get("timestamp"), label="block timestamp")
            if timestamp <= 0 or timestamp > 253_402_300_799:
                raise MarketHTTPError("Trueo swap block timestamp is outside the supported range.")
            timestamps[block_number] = float(timestamp)
        return timestamps

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Trueo {label} must be a finite non-negative timestamp.")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Trueo {label} must be a finite non-negative timestamp.") from exc
        if not math.isfinite(parsed) or parsed < 0 or parsed > 253_402_300_799:
            raise MarketConfigurationError(f"Trueo {label} must be a finite non-negative timestamp.")
        return parsed

    def _trade_limit(self, value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("Trueo trade history limit must be a positive integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Trueo trade history limit must be a positive integer.") from exc
        if parsed <= 0:
            raise MarketConfigurationError("Trueo trade history limit must be a positive integer.")
        return min(parsed, self.max_trade_logs)

    @classmethod
    def _block_number(cls, value: Any, *, label: str) -> int:
        if isinstance(value, bool) or value in (None, ""):
            raise MarketConfigurationError(f"Trueo {label} must be a non-negative block quantity.")
        try:
            text = str(value).strip()
            parsed = int(text, 0) if text.lower().startswith("0x") else int(text, 10)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Trueo {label} must be a non-negative block quantity.") from exc
        if parsed < 0:
            raise MarketConfigurationError(f"Trueo {label} must be a non-negative block quantity.")
        return parsed

    @classmethod
    def _topic_address(cls, value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", text):
            raise MarketConfigurationError(f"Trueo {label} must be a 32-byte indexed address topic.")
        return cls._address("0x" + text[-40:], label=label)

    def _read_market_summaries(self, addresses: List[str]) -> Dict[str, Dict[str, Any]]:
        calls: List[Tuple[str, str, str]] = []
        for offset, address in enumerate(addresses):
            calls.extend(
                [
                    (f"question:{offset}", address, SELECTORS["question"]),
                    (f"source:{offset}", address, SELECTORS["source"]),
                    (f"status:{offset}", address, SELECTORS["status"]),
                ]
            )
        values = self._batch_eth_calls(calls)
        summaries: Dict[str, Dict[str, Any]] = {}
        for offset, address in enumerate(addresses):
            question = values[f"question:{offset}"]
            source = values[f"source:{offset}"]
            status = values[f"status:{offset}"]
            if question is None or source is None or status is None:
                raise MarketConfigurationError("Trueo active market did not implement the summary ABI.")
            status_value = int(self._decode(status, ("uint256",))[0])
            summaries[address.casefold()] = {
                "address": address,
                "question": str(self._decode(question, ("string",))[0]),
                "source": str(self._decode(source, ("string",))[0]),
                "status": status_value,
                "status_name": self._status_name(status_value),
            }
        return summaries

    def _read_market(self, address: str) -> Dict[str, Any]:
        market = self._address(address, label="market address")
        values = self._batch_eth_calls(
            [
                ("active", self.manager_address, SELECTORS["is_active_market"] + self._address_arg(market)),
                ("version", market, SELECTORS["version"]),
                ("question", market, SELECTORS["question"]),
                ("source", market, SELECTORS["source"]),
                ("additional_info", market, SELECTORS["additional_info"]),
                ("end_of_trading", market, SELECTORS["end_of_trading"]),
                ("status", market, SELECTORS["status"]),
                ("winning_position", market, SELECTORS["winning_position"]),
                ("yes_token", market, SELECTORS["yes_token"]),
                ("no_token", market, SELECTORS["no_token"]),
                ("payment_token", market, SELECTORS["payment_token"]),
                ("v3_pools", market, SELECTORS["pools"]),
                ("v4_pool_ids", market, SELECTORS["pool_ids"]),
                ("v4_pool_keys", market, SELECTORS["pool_keys"]),
                ("v4_hook", market, SELECTORS["hook_address"]),
            ]
        )

        def required(key: str) -> str:
            value = values.get(key)
            if value is None:
                raise MarketConfigurationError(
                    f"Trueo market {market} does not implement the required official {key} ABI call."
                )
            return value

        if not bool(self._decode(required("active"), ("bool",))[0]):
            raise MarketConfigurationError("Trueo market address is not registered by the configured manager.")
        version = str(self._decode(required("version"), ("string",))[0]).strip()
        if not re.fullmatch(r"[12]\.[0-9]+\.[0-9]+", version):
            raise MarketConfigurationError(f"Trueo market returned unsupported contract version {version!r}.")
        row = {
            "address": market,
            "version": version,
            "question": str(self._decode(required("question"), ("string",))[0]),
            "source": str(self._decode(required("source"), ("string",))[0]),
            "additional_info": str(self._decode(required("additional_info"), ("string",))[0]),
            "end_of_trading": int(self._decode(required("end_of_trading"), ("uint256",))[0]),
            "status": int(self._decode(required("status"), ("uint256",))[0]),
            "winning_position": int(self._decode(required("winning_position"), ("uint256",))[0]),
            "yes_token": self._address(self._decode(required("yes_token"), ("address",))[0]),
            "no_token": self._address(self._decode(required("no_token"), ("address",))[0]),
            "payment_token": self._address(self._decode(required("payment_token"), ("address",))[0]),
        }
        if version.startswith("2."):
            yes_id, no_id = self._decode(required("v4_pool_ids"), ("bytes32", "bytes32"))
            pool_key_type = "(address,address,uint24,int24,address)"
            yes_key, no_key = self._decode(required("v4_pool_keys"), (pool_key_type, pool_key_type))
            row["amm_version"] = "v4"
            row["yes_pool_id"], row["no_pool_id"] = self._pool_id(yes_id), self._pool_id(no_id)
            row["yes_pool_key"] = self._validated_v4_pool_key(
                yes_key,
                row["yes_pool_id"],
                row["yes_token"],
                row["payment_token"],
            )
            row["no_pool_key"] = self._validated_v4_pool_key(
                no_key,
                row["no_pool_id"],
                row["no_token"],
                row["payment_token"],
            )
            hook_address = self._address(self._decode(required("v4_hook"), ("address",))[0])
            if int(hook_address[2:], 16) == 0:
                raise MarketConfigurationError("Trueo V4 market returned a zero hooks address.")
            if any(
                pool_key["hooks"].casefold() != hook_address.casefold()
                for pool_key in (row["yes_pool_key"], row["no_pool_key"])
            ):
                raise MarketConfigurationError("Trueo V4 PoolKey hooks do not match the market hook address.")
            if (
                row["yes_pool_key"]["fee"] != row["no_pool_key"]["fee"]
                or row["yes_pool_key"]["tick_spacing"] != row["no_pool_key"]["tick_spacing"]
            ):
                raise MarketConfigurationError("Trueo V4 YES/NO PoolKeys disagree on fee or tick spacing.")
            row["hook_address"] = hook_address
            row["pool_manager"] = self.v4_pool_manager_address
            row["state_view"] = self.v4_state_view_address
        else:
            yes_pool, no_pool = self._decode(required("v3_pools"), ("address", "address"))
            row["amm_version"] = "v3"
            row["yes_pool"], row["no_pool"] = self._address(yes_pool), self._address(no_pool)
        row["status_name"] = self._status_name(int(row["status"]))
        self._market_cache[market.casefold()] = row
        return row

    def _pool_price(self, pool: str, outcome_token: str, payment_token: str) -> Tuple[float, Dict[str, Any]]:
        token0 = self._call_address(pool, SELECTORS["token0"])
        token1 = self._call_address(pool, SELECTORS["token1"])
        slot0 = self._decode(self._call(pool, SELECTORS["slot0"]), ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"))
        sqrt_price = int(slot0[0])
        price, raw = self._price_from_sqrt_price(sqrt_price, token0, token1, outcome_token, payment_token)
        raw["tick"] = int(slot0[1])
        return price, raw

    def _v4_pool_price(self, pool_id: str, outcome_token: str, payment_token: str) -> Tuple[float, Dict[str, Any]]:
        token0, token1 = self._sorted_token_pair(outcome_token, payment_token)
        values = self._batch_eth_calls(
            [
                ("pool_manager", self.v4_state_view_address, SELECTORS["pool_manager"]),
                (
                    "slot0",
                    self.v4_state_view_address,
                    SELECTORS["v4_slot0"] + self._pool_id(pool_id)[2:],
                ),
            ]
        )
        if values["pool_manager"] is None or values["slot0"] is None:
            raise MarketConfigurationError("Trueo V4 StateView did not implement the required official ABI.")
        state_pool_manager = self._address(self._decode(values["pool_manager"], ("address",))[0])
        if state_pool_manager.casefold() != self.v4_pool_manager_address.casefold():
            raise MarketConfigurationError("Trueo V4 StateView is not bound to the configured PoolManager.")
        slot0 = self._decode(values["slot0"], ("uint160", "int24", "uint24", "uint24"))
        sqrt_price = int(slot0[0])
        price, raw = self._price_from_sqrt_price(sqrt_price, token0, token1, outcome_token, payment_token)
        raw.update(
            {
                "pool_id": self._pool_id(pool_id),
                "state_view": self.v4_state_view_address,
                "pool_manager": self.v4_pool_manager_address,
                "tick": int(slot0[1]),
                "protocol_fee": int(slot0[2]),
                "lp_fee": int(slot0[3]),
            }
        )
        return price, raw

    def _price_from_sqrt_price(
        self,
        sqrt_price: int,
        token0: str,
        token1: str,
        outcome_token: str,
        payment_token: str,
    ) -> Tuple[float, Dict[str, Any]]:
        if sqrt_price <= 0:
            raise MarketConfigurationError("Trueo pool returned a non-positive sqrt price.")
        decimals0 = self._token_decimals(token0)
        decimals1 = self._token_decimals(token1)
        raw_ratio = (sqrt_price * sqrt_price) / float(2**192)
        if token0.lower() == outcome_token.lower() and token1.lower() == payment_token.lower():
            price = raw_ratio * (10 ** (decimals0 - decimals1))
        elif token1.lower() == outcome_token.lower() and token0.lower() == payment_token.lower():
            price = (1.0 / raw_ratio) * (10 ** (decimals1 - decimals0))
        else:
            raise MarketConfigurationError("Trueo pool tokens do not match the market outcome/payment tokens.")
        if not math.isfinite(price) or price <= 0:
            raise MarketConfigurationError("Trueo pool returned an invalid outcome price.")
        return price, {"sqrt_price_x96": sqrt_price, "token0": token0, "token1": token1, "decimals0": decimals0, "decimals1": decimals1}

    def _token_decimals(self, token: str) -> int:
        key = token.lower()
        if key not in self._token_decimals_cache:
            value = int(self._call_uint(token, SELECTORS["decimals"]))
            if value < 0 or value > 36:
                raise MarketConfigurationError("Trueo token decimals are outside the supported range.")
            self._token_decimals_cache[key] = value
        return self._token_decimals_cache[key]

    @classmethod
    def _decode_signed_transaction(cls, value: Any) -> Dict[str, Any]:
        if (
            not isinstance(value, str)
            or len(value) < 2 + 65 * 2
            or len(value) > 2 + 1_000_000 * 2
            or not re.fullmatch(r"0x[0-9a-fA-F]+", value)
            or len(value) % 2
        ):
            raise MarketConfigurationError(
                "Trueo live orders require a canonical 0x-prefixed externally signed raw transaction."
            )
        raw = bytes.fromhex(value[2:])

        try:
            from eth_account._utils.legacy_transactions import Transaction
            from eth_account.typed_transactions import TypedTransaction
            from hexbytes import HexBytes
        except ImportError as exc:
            raise MarketConfigurationError(
                "Trueo live transaction verification requires the eth-account project dependency."
            ) from exc

        try:
            if raw[0] <= 0x7F:
                transaction = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
                transaction_type = int(transaction.get("type", raw[0]))
                chain_id = int(transaction["chainId"])
            else:
                transaction = Transaction.from_bytes(raw).as_dict()
                transaction_type = 0
                signature_v = int(transaction["v"])
                if signature_v < 35:
                    raise ValueError("legacy transaction is not EIP-155 chain protected")
                chain_id = (signature_v - 35) // 2

            target_raw = bytes(transaction["to"])
            if len(target_raw) != 20:
                raise ValueError("contract creation transactions are not supported")
            data_raw = bytes(transaction.get("data", b""))
            value_int = int(transaction.get("value", 0))
            if chain_id <= 0 or value_int < 0:
                raise ValueError("transaction chain ID or value is invalid")
        except Exception as exc:
            raise MarketConfigurationError(
                "Trueo signed_transaction could not be decoded as a chain-protected EVM transaction."
            ) from exc

        return {
            "chain_id": chain_id,
            "to": "0x" + target_raw.hex(),
            "data": "0x" + data_raw.hex(),
            "value": value_int,
            "type": transaction_type,
        }

    @staticmethod
    def _metadata_int(
        metadata: Mapping[str, Any],
        key: str,
        *,
        label: str,
        minimum: int = 1,
    ) -> int:
        value = metadata.get(key)
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Trueo live orders require an integer {label}.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Trueo live orders require an integer {label}.") from exc
        if parsed < minimum:
            raise MarketConfigurationError(f"Trueo live orders require {label} to be at least {minimum}.")
        return parsed

    @staticmethod
    def _calldata(value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]+", text) or len(text) % 2 or len(text) < 10:
            raise MarketConfigurationError(f"Trueo live orders require hexadecimal {label} with a 4-byte selector.")
        if len(text) > 2 + 1_000_000 * 2:
            raise MarketConfigurationError(f"Trueo {label} exceeds the 1 MB safety limit.")
        return "0x" + text[2:].lower()

    @classmethod
    def _validate_reviewed_number(
        cls,
        metadata: Mapping[str, Any],
        key: str,
        expected: Any,
        *,
        label: str,
    ) -> None:
        reviewed = cls._finite_float(metadata.get(key), f"reviewed {label}")
        requested = cls._finite_float(expected, label)
        if reviewed != requested:
            raise MarketConfigurationError(f"Trueo signed transaction metadata uses a different {label}.")

    @classmethod
    def _validate_reviewed_limit_price(cls, metadata: Mapping[str, Any], expected: Any) -> None:
        reviewed = metadata.get("limit_price")
        if expected is None:
            if reviewed is not None:
                raise MarketConfigurationError("Trueo signed transaction metadata uses a different limit price.")
            return
        cls._validate_reviewed_number(metadata, "limit_price", expected, label="limit price")

    def _rpc(
        self,
        method: str,
        params: List[Any],
        *,
        max_response_bytes: Optional[int] = None,
    ) -> Any:
        request_options: Dict[str, Any] = {
            "json_body": {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            "headers": {},
        }
        if max_response_bytes is not None:
            request_options["max_response_bytes"] = max_response_bytes
        for attempt in range(self.rpc_max_retries + 1):
            try:
                payload = self.runtime.request_json("POST", self.rpc_url, **request_options)
            except MarketHTTPError as exc:
                if self._is_rate_limit_error(exc) and attempt < self.rpc_max_retries:
                    time.sleep(self.rpc_retry_backoff_seconds * (2**attempt))
                    continue
                raise
            if not isinstance(payload, Mapping):
                raise MarketHTTPError("Trueo RPC response was not a JSON object.")
            if payload.get("error"):
                if self._is_rate_limit_error(payload["error"]) and attempt < self.rpc_max_retries:
                    time.sleep(self.rpc_retry_backoff_seconds * (2**attempt))
                    continue
                raise MarketHTTPError(f"Trueo RPC error: {payload['error']}")
            return payload.get("result")
        raise MarketHTTPError("Trueo RPC remained rate-limited after bounded retries.")

    def _batch_eth_calls(self, calls: List[Tuple[str, str, str]]) -> Dict[str, Optional[str]]:
        """Execute one bounded JSON-RPC batch and retain per-call reverts.

        V1 and V2 market contracts intentionally expose different pool getters,
        so one of those optional calls is expected to revert. All common fields
        and the manager-membership proof are validated by the caller.
        """

        if not calls or len({name for name, _, _ in calls}) != len(calls):
            raise MarketConfigurationError("Trueo internal batch call names must be non-empty and unique.")
        requests = []
        names: Dict[int, str] = {}
        for request_id, (name, address, data) in enumerate(calls, start=1):
            if not re.fullmatch(r"[0-9a-fA-F]+", data) or len(data) % 2:
                raise MarketConfigurationError("Trueo internal batch call data was not canonical hexadecimal.")
            names[request_id] = name
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "eth_call",
                    "params": [{"to": self._address(address), "data": "0x" + data}, "latest"],
                }
            )
        payloads = self._batch_rpc_requests(requests)
        responses: Dict[int, Optional[str]] = {}
        for request_id, result in payloads.items():
            if result is None:
                responses[request_id] = None
            elif isinstance(result, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", result):
                responses[request_id] = result
            else:
                raise MarketHTTPError("Trueo batch eth_call returned non-canonical hex data.")
        return {names[request_id]: responses[request_id] for request_id in names}

    def _batch_rpc_requests(self, requests: List[Dict[str, Any]]) -> Dict[int, Any]:
        if not requests:
            return {}
        expected_ids = {int(request["id"]) for request in requests}
        if len(expected_ids) != len(requests):
            raise MarketConfigurationError("Trueo JSON-RPC batch ids must be unique.")
        responses: Dict[int, Any] = {}
        for offset in range(0, len(requests), self.batch_call_size):
            pending = requests[offset : offset + self.batch_call_size]
            for attempt in range(self.rpc_max_retries + 1):
                try:
                    payload = self.runtime.request_json(
                        "POST",
                        self.rpc_url,
                        json_body=pending,
                        headers={},
                        max_response_bytes=self.max_rpc_response_bytes,
                    )
                except MarketHTTPError as exc:
                    if self._is_rate_limit_error(exc) and attempt < self.rpc_max_retries:
                        time.sleep(self.rpc_retry_backoff_seconds * (2**attempt))
                        continue
                    raise
                if isinstance(payload, Mapping) and payload.get("error") is not None:
                    if self._is_rate_limit_error(payload["error"]):
                        if attempt < self.rpc_max_retries:
                            time.sleep(self.rpc_retry_backoff_seconds * (2**attempt))
                            continue
                        raise MarketHTTPError(
                            "Trueo JSON-RPC batch remained rate-limited after bounded retries."
                        )
                    raise MarketHTTPError(f"Trueo JSON-RPC batch error: {payload['error']}")
                if not isinstance(payload, list):
                    raise MarketHTTPError("Trueo JSON-RPC batch response was not a JSON array.")
                pending_ids = {int(request["id"]) for request in pending}
                retry_ids: set[int] = set()
                seen_ids: set[int] = set()
                for item in payload:
                    if not isinstance(item, Mapping) or isinstance(item.get("id"), bool):
                        raise MarketHTTPError("Trueo JSON-RPC batch contained a malformed response item.")
                    try:
                        request_id = int(item.get("id"))
                    except (TypeError, ValueError) as exc:
                        raise MarketHTTPError("Trueo JSON-RPC batch contained a malformed response id.") from exc
                    if request_id not in pending_ids or request_id in seen_ids or request_id in responses:
                        raise MarketHTTPError("Trueo JSON-RPC batch contained an unknown or duplicate response id.")
                    seen_ids.add(request_id)
                    if item.get("error") is not None:
                        if self._is_rate_limit_error(item["error"]):
                            retry_ids.add(request_id)
                        else:
                            responses[request_id] = None
                        continue
                    responses[request_id] = item.get("result")
                if seen_ids != pending_ids:
                    raise MarketHTTPError("Trueo JSON-RPC batch response omitted one or more requested calls.")
                if not retry_ids:
                    break
                if attempt >= self.rpc_max_retries:
                    raise MarketHTTPError("Trueo JSON-RPC batch remained rate-limited after bounded retries.")
                pending = [request for request in pending if int(request["id"]) in retry_ids]
                time.sleep(self.rpc_retry_backoff_seconds * (2**attempt))
        if set(responses) != expected_ids:
            raise MarketHTTPError("Trueo JSON-RPC batch response omitted one or more requested calls.")
        return responses

    @staticmethod
    def _is_rate_limit_error(value: Any) -> bool:
        text = str(value or "").casefold()
        return "429" in text or "rate limit" in text or "over rate" in text or "-32016" in text

    def _call(self, address: str, data: str) -> str:
        result = self._rpc("eth_call", [{"to": self._address(address), "data": "0x" + data}, "latest"])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise MarketHTTPError("Trueo eth_call did not return hex data.")
        return result

    def _call_uint(self, address: str, data: str) -> int:
        values = self._decode(self._call(address, data), ("uint256",))
        return int(values[0])

    def _call_address(self, address: str, data: str) -> str:
        values = self._decode(self._call(address, data), ("address",))
        return self._address(values[0])

    def _call_result_address(self, result: str) -> str:
        values = self._decode(result, ("address",))
        return self._address(values[0])

    def _call_string(self, address: str, data: str) -> str:
        values = self._decode(self._call(address, data), ("string",))
        return str(values[0])

    @staticmethod
    def _decode(value: str, types: Tuple[str, ...]) -> Tuple[Any, ...]:
        try:
            from eth_abi import decode
            from eth_abi.exceptions import DecodingError
        except ImportError as exc:
            raise MarketConfigurationError("Trueo ABI decoding requires the eth-abi project dependency.") from exc
        try:
            return tuple(decode(list(types), bytes.fromhex(value[2:])))
        except (ValueError, TypeError, OverflowError, DecodingError) as exc:
            raise MarketConfigurationError("Trueo RPC returned data that did not match the documented ABI.") from exc

    @staticmethod
    def _uint_arg(value: int) -> str:
        return f"{int(value):064x}"

    @classmethod
    def _address_arg(cls, value: Any) -> str:
        return cls._address(value)[2:].lower().rjust(64, "0")

    @staticmethod
    def _pool_id(value: Any) -> str:
        if isinstance(value, (bytes, bytearray)):
            text = "0x" + bytes(value).hex()
        else:
            text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", text) or int(text[2:], 16) == 0:
            raise MarketConfigurationError("Trueo V4 pool id must be a non-zero 32-byte hex value.")
        return "0x" + text[2:].lower()

    @classmethod
    def _sorted_token_pair(cls, first: Any, second: Any) -> Tuple[str, str]:
        left = cls._address(first, label="pool token")
        right = cls._address(second, label="pool token")
        if left.casefold() == right.casefold():
            raise MarketConfigurationError("Trueo pool tokens must be distinct.")
        return (left, right) if int(left[2:], 16) < int(right[2:], 16) else (right, left)

    @classmethod
    def _validated_v4_pool_key(
        cls,
        value: Any,
        pool_id: str,
        outcome_token: str,
        payment_token: str,
    ) -> Dict[str, Any]:
        if not isinstance(value, (list, tuple)) or len(value) != 5:
            raise MarketConfigurationError("Trueo V4 market returned a malformed PoolKey.")
        currency0 = cls._address(value[0], label="V4 currency0")
        currency1 = cls._address(value[1], label="V4 currency1")
        expected0, expected1 = cls._sorted_token_pair(outcome_token, payment_token)
        if currency0.casefold() != expected0.casefold() or currency1.casefold() != expected1.casefold():
            raise MarketConfigurationError("Trueo V4 PoolKey currencies do not match the market tokens.")
        fee = int(value[2])
        tick_spacing = int(value[3])
        hooks = cls._address(value[4], label="V4 hooks address")
        if fee < 0 or fee > 0xFFFFFF or tick_spacing <= 0 or tick_spacing > 0x7FFF:
            raise MarketConfigurationError("Trueo V4 PoolKey fee or tick spacing is outside the supported range.")
        try:
            from eth_abi import encode
            from eth_utils import keccak
        except ImportError as exc:
            raise MarketConfigurationError("Trueo V4 PoolKey verification requires project ABI dependencies.") from exc
        encoded = encode(
            ["address", "address", "uint24", "int24", "address"],
            [currency0, currency1, fee, tick_spacing, hooks],
        )
        computed_id = "0x" + keccak(encoded).hex()
        if computed_id.casefold() != cls._pool_id(pool_id).casefold():
            raise MarketConfigurationError("Trueo V4 PoolKey does not hash to the advertised pool id.")
        return {
            "currency0": currency0,
            "currency1": currency1,
            "fee": fee,
            "tick_spacing": tick_spacing,
            "hooks": hooks,
        }

    @classmethod
    def _address(cls, value: Any, *, label: str = "address") -> str:
        text = str(value or "").strip()
        if not ADDRESS_RE.fullmatch(text):
            raise MarketConfigurationError(f"Trueo {label} must be a 20-byte hex address.")
        return text

    @classmethod
    def _split_contract_id(cls, value: Any) -> Tuple[str, int]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) not in {0, 1}:
            raise MarketConfigurationError("Trueo contract id must be '<market-address>:0' (YES) or ':1' (NO).")
        return cls._address(parts[0], label="market address"), int(parts[1])

    @staticmethod
    def _status_name(value: int) -> str:
        return {
            0: "created",
            1: "open_for_resolution",
            2: "resolution_proposed",
            3: "dispute_raised",
            4: "set_by_council",
            5: "reset_by_council",
            6: "escalated_dispute_raised",
            7: "finalized",
        }.get(value, f"unknown:{value}")
