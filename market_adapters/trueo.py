from __future__ import annotations

import math
import re
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
    "slot0": "3850c7bd",
    "token0": "0dfe1681",
    "token1": "d21220a7",
    "decimals": "313ce567",
}

UNISWAP_V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
DEFAULT_TRUEO_LOG_WINDOW_BLOCKS = 50_000
MAX_TRUEO_LOG_WINDOW_BLOCKS = 500_000
DEFAULT_TRUEO_MAX_TRADE_LOGS = 500
MAX_TRUEO_MAX_TRADE_LOGS = 5_000


class TrueoAdapter(MarketAdapter):
    """Official Trueo Base on-chain adapter.

    Trueo publishes no hosted market-data API: the supported integration is the
    deployed ``TruthMarketManager`` and each market's immutable on-chain fields.
    This adapter reads the manager/market contracts through JSON-RPC, derives a
    current YES/NO AMM price from the documented Uniswap V3 pools, and keeps
    paper trading local. Live execution accepts only a complete, externally
    signed raw transaction and remains disabled unless the operator enables the
    explicit submission gate.
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
                "chain_id": self.chain_id,
                "network": "Base mainnet",
                "references": list(TRUEO_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "allowlisted_live_transaction_target_count": len(self.live_transaction_targets),
                "wallet_transaction_required": True,
                "settlement_required": True,
                "trade_history_source": "uniswap_v3_swap_logs",
                "trade_history_bounded": True,
                "log_window_blocks": self.log_window_blocks,
                "max_trade_logs": self.max_trade_logs,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        count = self._call_uint(self.manager_address, SELECTORS["active_count"])
        rows: List[MarketEvent] = []
        needle = str(query or "").strip().lower()
        self._market_cache = {}
        for index in range(min(count, desired * 4)):
            address = self._call_address(
                self.manager_address,
                SELECTORS["active_market"] + self._uint_arg(index),
            )
            row = self._read_market(address)
            title = str(row.get("question") or address)
            if needle and needle not in title.lower() and needle not in str(row.get("source") or "").lower():
                continue
            self._market_cache[address.lower()] = row
            rows.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=address,
                    title=title,
                    url=f"https://basescan.org/address/{address}",
                    status=str(row.get("status_name") or "unknown"),
                    raw=dict(row),
                )
            )
            if len(rows) >= desired:
                break
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
        pool = row["yes_pool"] if outcome_index == 0 else row["no_pool"]
        payment_token = row["payment_token"]
        outcome_token = row["yes_token"] if outcome_index == 0 else row["no_token"]
        value, raw = self._pool_price(pool, outcome_token, payment_token)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=f"{market_address}:{outcome_index}",
            last=value,
            midpoint=value,
            source="trueo_uniswap_v3_slot0",
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
        """Return bounded public Uniswap V3 swaps for one Trueo outcome pool.

        Trueo routes outcome trades through the Uniswap V3 pools created by its
        official market contracts.  The pool ``Swap`` event contains signed
        token deltas, so this method derives the outcome-side BUY/SELL, filled
        outcome size, and executed collateral price without pretending that a
        router ``sender`` is the end-user wallet.  The block range and result
        count are deliberately bounded to keep a public RPC endpoint from
        becoming an unbounded history proxy.
        """

        self.ensure_capability("trade_history")
        market_address, outcome_index = self._split_contract_id(contract_id)
        row = self._market_cache.get(market_address.lower()) or self._read_market(market_address)
        pool = row["yes_pool"] if outcome_index == 0 else row["no_pool"]
        outcome_token = row["yes_token"] if outcome_index == 0 else row["no_token"]
        payment_token = row["payment_token"]
        desired = self._trade_limit(limit)
        after_ts = self._history_timestamp(after, "after") if after is not None else 0.0
        before_ts = self._history_timestamp(before, "before") if before is not None else 253_402_300_799.0
        if before_ts < after_ts:
            raise MarketConfigurationError("Trueo trade history requires before to be at or after after.")

        from_block, to_block = self._trade_log_block_bounds()
        logs = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": pool,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "topics": [UNISWAP_V3_SWAP_TOPIC],
                }
            ],
        )
        if not isinstance(logs, list):
            raise MarketHTTPError("Trueo eth_getLogs did not return a list.")
        if len(logs) > self.max_trade_logs:
            raise MarketHTTPError(
                f"Trueo RPC returned {len(logs)} swap logs, exceeding the configured safety limit of {self.max_trade_logs}."
            )

        token0 = self._call_address(pool, SELECTORS["token0"])
        token1 = self._call_address(pool, SELECTORS["token1"])
        decimals0 = self._token_decimals(token0)
        decimals1 = self._token_decimals(token1)
        if {token0.lower(), token1.lower()} != {outcome_token.lower(), payment_token.lower()}:
            raise MarketConfigurationError("Trueo swap pool tokens do not match the market outcome/payment tokens.")

        block_timestamps: Dict[int, float] = {}
        canonical = f"{market_address}:{outcome_index}"
        trades: List[MarketTrade] = []
        for log in logs:
            decoded = self._decode_swap_log(log, pool)
            if decoded is None:
                continue
            timestamp = block_timestamps.get(decoded["block_number"])
            if timestamp is None:
                timestamp = self._block_timestamp(decoded["block_number"])
                block_timestamps[decoded["block_number"]] = timestamp
            if timestamp < after_ts or timestamp > before_ts:
                continue

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
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=f"{decoded['transaction_hash']}:{decoded['log_index']}",
                    side="BUY" if outcome_delta < 0 else "SELL",
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw={
                        "source": "trueo_uniswap_v3_swap",
                        "pool": pool,
                        "market_address": market_address,
                        "outcome_index": outcome_index,
                        "sender": decoded["sender"],
                        "recipient": decoded["recipient"],
                        "amount0": decoded["amount0"],
                        "amount1": decoded["amount1"],
                        "sqrt_price_x96": decoded["sqrt_price_x96"],
                        "liquidity": decoded["liquidity"],
                        "tick": decoded["tick"],
                        "block_number": decoded["block_number"],
                        "transaction_hash": decoded["transaction_hash"],
                        "log_index": decoded["log_index"],
                        "token0": token0,
                        "token1": token1,
                        "outcome_token": outcome_token,
                        "payment_token": payment_token,
                    },
                )
            )

        trades.sort(key=lambda trade: (float(trade.timestamp or 0), trade.trade_id), reverse=True)
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
        for timestamp, bucket_trades in sorted(buckets.items()):
            ordered = sorted(bucket_trades, key=lambda trade: (float(trade.timestamp or 0), trade.trade_id))
            prices = [float(trade.price) for trade in ordered]
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=contract_id,
                    timestamp=float(timestamp),
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=sum(float(trade.size) for trade in ordered),
                    raw={
                        "source": "trueo_uniswap_v3_swap",
                        "resolution": resolution_key,
                        "trade_count": len(ordered),
                        "trade_ids": [trade.trade_id for trade in ordered],
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

    def _trade_log_block_bounds(self) -> Tuple[int, int]:
        configured_to = self.config.get("trueo_log_to_block")
        to_block = (
            self._block_number(configured_to, label="trueo_log_to_block")
            if configured_to not in (None, "")
            else self._block_number(self._rpc("eth_blockNumber", []), label="latest block")
        )
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

    def _decode_swap_log(self, log: Any, pool: str) -> Optional[Dict[str, Any]]:
        if not isinstance(log, Mapping):
            return None
        address = self._address(log.get("address"), label="swap log address")
        if address.casefold() != pool.casefold():
            return None
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            return None
        if str(topics[0]).casefold() != UNISWAP_V3_SWAP_TOPIC.casefold():
            return None
        sender = self._topic_address(topics[1], label="swap sender")
        recipient = self._topic_address(topics[2], label="swap recipient")
        data = log.get("data")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", data) or len(data) % 2:
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

    def _block_timestamp(self, block_number: int) -> float:
        payload = self._rpc("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Trueo RPC did not return a block for a swap log.")
        timestamp = self._block_number(payload.get("timestamp"), label="block timestamp")
        if timestamp <= 0 or timestamp > 253_402_300_799:
            raise MarketHTTPError("Trueo swap block timestamp is outside the supported range.")
        return float(timestamp)

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

    def _read_market(self, address: str) -> Dict[str, Any]:
        row = {
            "address": address,
            "question": self._call_string(address, SELECTORS["question"]),
            "source": self._call_string(address, SELECTORS["source"]),
            "additional_info": self._call_string(address, SELECTORS["additional_info"]),
            "end_of_trading": self._call_uint(address, SELECTORS["end_of_trading"]),
            "status": self._call_uint(address, SELECTORS["status"]),
            "winning_position": self._call_uint(address, SELECTORS["winning_position"]),
            "yes_token": self._call_address(address, SELECTORS["yes_token"]),
            "no_token": self._call_address(address, SELECTORS["no_token"]),
            "payment_token": self._call_address(address, SELECTORS["payment_token"]),
        }
        pools = self._call(address, SELECTORS["pools"])
        decoded = self._decode(pools, ("address", "address"))
        row["yes_pool"], row["no_pool"] = self._address(decoded[0]), self._address(decoded[1])
        row["status_name"] = self._status_name(int(row["status"]))
        return row

    def _pool_price(self, pool: str, outcome_token: str, payment_token: str) -> Tuple[float, Dict[str, Any]]:
        token0 = self._call_address(pool, SELECTORS["token0"])
        token1 = self._call_address(pool, SELECTORS["token1"])
        slot0 = self._decode(self._call(pool, SELECTORS["slot0"]), ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"))
        sqrt_price = int(slot0[0])
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

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Trueo RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Trueo RPC error: {payload['error']}")
        return payload.get("result")

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

    def _call_string(self, address: str, data: str) -> str:
        values = self._decode(self._call(address, data), ("string",))
        return str(values[0])

    @staticmethod
    def _decode(value: str, types: Tuple[str, ...]) -> Tuple[Any, ...]:
        try:
            from eth_abi import decode

            return tuple(decode(list(types), bytes.fromhex(value[2:])))
        except (ImportError, ValueError, TypeError, OverflowError) as exc:
            raise MarketConfigurationError("Trueo RPC returned data that did not match the documented ABI.") from exc

    @staticmethod
    def _uint_arg(value: int) -> str:
        return f"{int(value):064x}"

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
