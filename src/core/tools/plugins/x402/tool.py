"""x402 protocol client — pay for HTTP 402-gated resources with USDC.

Flow:
1. GET url → 402 response with payment metadata
2. Parse payment requirements (address, amount, chain, token)
3. Sign + send USDC via wallet keystore
4. Retry GET with payment proof headers
5. Return gated content
"""

import json
import logging
from typing import Dict, Optional, Tuple

import aiohttp

from src.core.tools.base import BaseTool
from src.core.types import ToolResult

from ..wallet.keystore import WalletKeystore
from ..wallet.chains.base_chain import BaseChain

logger = logging.getLogger(__name__)

# Default safety cap — max USDC per single x402 payment
DEFAULT_MAX_AMOUNT = 1.0


class X402Tool(BaseTool):
    """Pay for x402-gated HTTP resources using USDC."""

    name = "x402"
    description = (
        "x402 protocol client. Operations: "
        "'check' probes if a URL requires payment (HTTP 402), "
        "'pay_and_fetch' pays the required USDC and retrieves the gated content."
    )
    parameters = {
        "operation": {
            "type": "string",
            "description": "Operation: 'check' or 'pay_and_fetch'",
            "enum": ["check", "pay_and_fetch"],
        },
        "url": {
            "type": "string",
            "description": "The URL to access or check",
        },
        "max_amount": {
            "type": "number",
            "description": "Maximum USDC willing to pay (safety cap, default 1.0)",
        },
    }

    def __init__(self, encryption_key: str = ""):
        if not encryption_key:
            logger.warning("x402 plugin: no WALLET_ENCRYPTION_KEY — payments disabled")
            self._keystore = None
        else:
            self._keystore = WalletKeystore(encryption_key)
        self._base_chain = BaseChain()

    async def execute(self, operation: str = "", url: str = "", max_amount: float = DEFAULT_MAX_AMOUNT, **kwargs) -> ToolResult:
        if not url:
            return ToolResult(success=False, error="Missing 'url' parameter")

        try:
            if operation == "check":
                return await self._check_402(url)
            elif operation == "pay_and_fetch":
                if not self._keystore:
                    return ToolResult(success=False, error="Wallet not configured — set WALLET_ENCRYPTION_KEY")
                return await self._pay_and_fetch(url, max_amount)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"x402 {operation} failed for {url}: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    async def _check_402(self, url: str) -> ToolResult:
        """Probe a URL to check if it's 402-gated."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status == 402:
                    payment_info = await self._parse_payment_info(resp)
                    return ToolResult(
                        success=True,
                        output=(
                            f"URL is payment-gated (HTTP 402).\n"
                            f"  Amount: {payment_info.get('amount', 'unknown')} {payment_info.get('token', 'USDC')}\n"
                            f"  Chain: {payment_info.get('chain', 'unknown')}\n"
                            f"  Recipient: {payment_info.get('address', 'unknown')}"
                        ),
                        metadata=payment_info,
                    )
                else:
                    return ToolResult(
                        success=True,
                        output=f"URL returned HTTP {resp.status} — not payment-gated.",
                        metadata={"status": resp.status, "gated": False},
                    )

    async def _pay_and_fetch(self, url: str, max_amount: float) -> ToolResult:
        """Full x402 flow: detect → pay → fetch."""
        async with aiohttp.ClientSession() as session:
            # Step 1: Initial request
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status != 402:
                    # Not gated — return content directly
                    body = await resp.text()
                    return ToolResult(
                        success=True,
                        output=f"URL not gated (HTTP {resp.status}). Content:\n{body[:2000]}",
                    )

                # Step 2: Parse payment requirements
                payment_info = await self._parse_payment_info(resp)

            amount = payment_info.get("amount", 0)
            address = payment_info.get("address", "")
            chain = payment_info.get("chain", "base")

            if not address:
                return ToolResult(success=False, error="402 response missing payment address")
            if not amount:
                return ToolResult(success=False, error="402 response missing payment amount")

            # Step 3: Safety check
            if float(amount) > max_amount:
                return ToolResult(
                    success=False,
                    error=f"Payment amount {amount} USDC exceeds safety cap of {max_amount} USDC. "
                          f"Increase max_amount to proceed.",
                )

            # Step 4: Only Base chain supported for x402 payments currently
            if chain not in ("base", "ethereum", "evm"):
                return ToolResult(
                    success=False,
                    error=f"x402 payment on chain '{chain}' not yet supported. Only Base/EVM.",
                )

            # Step 5: Sign and send payment
            private_key = self._keystore.get_private_key("base")
            if not private_key:
                return ToolResult(
                    success=False,
                    error="No Base wallet found. Generate one first with the wallet tool.",
                )

            tx_hash = await self._base_chain.send_usdc(private_key, address, float(amount))

            # Step 6: Also sign a receipt message for proof
            sender_address = self._keystore.get_address("base")
            signature = await self._base_chain.sign_message(
                private_key,
                f"x402-payment:{tx_hash}:{url}",
            )

            # Step 7: Retry with payment proof headers
            payment_headers = {
                "X-Payment-TxHash": tx_hash,
                "X-Payment-Signature": signature,
                "X-Payment-Address": sender_address,
                "X-Payment-Chain": "base",
            }

            async with session.get(url, headers=payment_headers) as paid_resp:
                if paid_resp.status == 200:
                    body = await paid_resp.text()
                    return ToolResult(
                        success=True,
                        output=f"Payment successful ({amount} USDC). Content:\n{body[:4000]}",
                        metadata={
                            "tx_hash": tx_hash,
                            "amount": amount,
                            "chain": "base",
                        },
                    )
                else:
                    return ToolResult(
                        success=False,
                        error=(
                            f"Payment sent (tx: {tx_hash}) but access still denied "
                            f"(HTTP {paid_resp.status}). The service may need time to confirm."
                        ),
                        metadata={"tx_hash": tx_hash},
                    )

    async def _parse_payment_info(self, resp: aiohttp.ClientResponse) -> Dict:
        """Extract payment requirements from a 402 response.

        Checks both headers (X-Payment-*) and JSON body.
        """
        info = {}

        # Try headers first (standard x402 convention)
        info["address"] = resp.headers.get("X-Payment-Address", "")
        info["amount"] = resp.headers.get("X-Payment-Amount", "")
        info["chain"] = resp.headers.get("X-Payment-Chain", "base")
        info["token"] = resp.headers.get("X-Payment-Token", "USDC")

        # Try JSON body as fallback
        if not info["address"] or not info["amount"]:
            try:
                body = await resp.json()
                if isinstance(body, dict):
                    payment = body.get("payment", body)
                    info["address"] = info["address"] or payment.get("address", "")
                    info["amount"] = info["amount"] or payment.get("amount", "")
                    info["chain"] = info["chain"] or payment.get("chain", "base")
                    info["token"] = info["token"] or payment.get("token", "USDC")
                    if "description" in payment:
                        info["description"] = payment["description"]
            except Exception:
                pass

        # Convert amount to float if string
        try:
            info["amount"] = float(info["amount"]) if info["amount"] else 0
        except (ValueError, TypeError):
            info["amount"] = 0

        info["gated"] = True
        return info
