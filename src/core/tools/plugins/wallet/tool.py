"""Wallet plugin — multi-chain crypto wallet for Base and Solana USDC.

All transfers require explicit owner approval via confirmation code.
"""

import asyncio
import logging
import os
import secrets
import time
from typing import Dict, Optional, Tuple

from src.core.tools.base import BaseTool
from src.core.types import ToolResult

from src.core.tools.plugins.wallet.keystore import WalletKeystore
from src.core.tools.plugins.wallet.chains.base_chain import BaseChain
from src.core.tools.plugins.wallet.chains.solana_chain import SolanaChain
from src.core.tools.plugins.wallet.ledger import WalletLedger

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("base", "solana")

# Owner's personal wallet addresses — sweep destination
OWNER_BASE_ADDRESS = os.getenv("OWNER_BASE_ADDRESS", "")
OWNER_SOLANA_ADDRESS = os.getenv("OWNER_SOLANA_ADDRESS", "")

# Spending limits — configurable via env vars
MAX_SEND_PER_TX = float(os.getenv("WALLET_MAX_PER_TX", "10"))       # Max USDC per single send
MAX_SEND_PER_DAY = float(os.getenv("WALLET_MAX_PER_DAY", "50"))     # Max USDC per 24h window

# Auto-sweep: forward USDC to owner when balance exceeds threshold
SWEEP_THRESHOLD = float(os.getenv("WALLET_SWEEP_THRESHOLD", "50"))   # Trigger sweep above this USDC
SWEEP_KEEP = float(os.getenv("WALLET_SWEEP_KEEP", "5"))              # Keep this much USDC as gas buffer

# Approval expiry — pending transfers expire after this many seconds
APPROVAL_EXPIRY_SECONDS = 300  # 5 minutes


class WalletTool(BaseTool):
    """Multi-chain crypto wallet — check balance, send USDC, sign messages."""

    name = "wallet"
    description = (
        "Crypto wallet for Base and Solana chains. "
        "Operations: generate (create keypair), address (show address), "
        "balance (check ETH/SOL + USDC), send (request USDC transfer — requires owner approval), "
        "confirm_send (execute a previously approved transfer), "
        "sweep (forward USDC above threshold to owner's personal wallet), "
        "ledger (view transaction history), "
        "sign (sign a message), tx_status (check transaction)."
    )
    parameters = {
        "operation": {
            "type": "string",
            "description": "Wallet operation to perform",
            "enum": ["generate", "address", "balance", "send", "confirm_send", "sweep", "ledger", "sign", "tx_status"],
        },
        "chain": {
            "type": "string",
            "description": "Blockchain: 'base' or 'solana'",
            "enum": ["base", "solana"],
        },
        "to": {
            "type": "string",
            "description": "Recipient address (required for 'send')",
        },
        "amount": {
            "type": "number",
            "description": "USDC amount to send (required for 'send')",
        },
        "message": {
            "type": "string",
            "description": "Message to sign (required for 'sign')",
        },
        "tx_hash": {
            "type": "string",
            "description": "Transaction hash to check (required for 'tx_status')",
        },
        "approval_code": {
            "type": "string",
            "description": "6-digit approval code from owner (required for 'confirm_send')",
        },
    }

    def __init__(self, encryption_key: str = ""):
        if not encryption_key:
            logger.warning("Wallet plugin: no WALLET_ENCRYPTION_KEY — wallet disabled")
            self._keystore = None
        else:
            self._keystore = WalletKeystore(encryption_key)
        self._base = BaseChain()
        self._solana = SolanaChain()
        self._ledger = WalletLedger()
        # Spending tracker: list of (timestamp, amount) for rolling 24h window
        self._spend_log: list = []
        # Pending transfers awaiting owner approval: {code: {chain, to, amount, created_at}}
        self._pending_transfers: Dict[str, dict] = {}

    def _chain_adapter(self, chain: str):
        if chain == "base":
            return self._base
        elif chain == "solana":
            return self._solana
        raise ValueError(f"Unsupported chain: {chain}")

    async def execute(self, operation: str = "", chain: str = "base", **kwargs) -> ToolResult:
        if not self._keystore:
            return ToolResult(success=False, error="Wallet not configured — set WALLET_ENCRYPTION_KEY")

        if operation not in ("confirm_send",) and chain not in SUPPORTED_CHAINS:
            return ToolResult(success=False, error=f"Unsupported chain: {chain}. Use: {SUPPORTED_CHAINS}")

        try:
            if operation == "generate":
                return await self._generate(chain)
            elif operation == "address":
                return await self._address(chain)
            elif operation == "balance":
                return await self._balance(chain)
            elif operation == "send":
                return await self._request_send(chain, kwargs.get("to", ""), kwargs.get("amount", 0))
            elif operation == "confirm_send":
                return await self._confirm_send(kwargs.get("approval_code", ""))
            elif operation == "sweep":
                return await self._sweep(chain)
            elif operation == "ledger":
                return await self._ledger_query(chain, kwargs.get("limit", 10))
            elif operation == "sign":
                return await self._sign(chain, kwargs.get("message", ""))
            elif operation == "tx_status":
                return await self._tx_status(chain, kwargs.get("tx_hash", ""))
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"Wallet {operation} on {chain} failed: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    async def _generate(self, chain: str) -> ToolResult:
        address = self._keystore.generate_keypair(chain)
        return ToolResult(
            success=True,
            output=f"Generated {chain} wallet: {address}",
            metadata={"chain": chain, "address": address},
        )

    async def _address(self, chain: str) -> ToolResult:
        address = self._keystore.get_address(chain)
        if not address:
            return ToolResult(success=False, error=f"No {chain} wallet found. Use operation='generate' first.")
        return ToolResult(
            success=True,
            output=f"{chain} address: {address}",
            metadata={"chain": chain, "address": address},
        )

    async def _balance(self, chain: str) -> ToolResult:
        address = self._keystore.get_address(chain)
        if not address:
            return ToolResult(success=False, error=f"No {chain} wallet. Generate one first.")

        adapter = self._chain_adapter(chain)
        balances = await adapter.get_balance(address)

        if chain == "base":
            output = f"Base wallet ({address}):\n  ETH: {balances['eth']}\n  USDC: {balances['usdc']}"
        else:
            output = f"Solana wallet ({address}):\n  SOL: {balances['sol']}\n  USDC: {balances['usdc']}"

        # Check if auto-sweep is needed
        usdc_balance = float(balances.get("usdc", 0))
        sweep_note = ""
        if usdc_balance > SWEEP_THRESHOLD:
            owner_addr = OWNER_BASE_ADDRESS if chain == "base" else OWNER_SOLANA_ADDRESS
            if owner_addr:
                sweep_note = (
                    f"\n\n⚠️ USDC balance ({usdc_balance:.2f}) exceeds sweep threshold ({SWEEP_THRESHOLD})."
                    f"\nUse operation='sweep' to forward excess to owner's wallet."
                )

        return ToolResult(success=True, output=output + sweep_note, metadata={"chain": chain, **balances})

    def _check_spending_limits(self, to: str, amount: float) -> str:
        """Check per-tx and daily spending limits. Returns error message or empty string."""
        # Owner addresses bypass limits (sweeps to owner's personal wallet)
        owner_addresses = {a.lower() for a in [OWNER_BASE_ADDRESS, OWNER_SOLANA_ADDRESS] if a}
        if owner_addresses and to.lower() in owner_addresses:
            return ""

        # Per-transaction limit
        if amount > MAX_SEND_PER_TX:
            return (
                f"Amount {amount} USDC exceeds per-transaction limit of {MAX_SEND_PER_TX} USDC. "
                f"Set WALLET_MAX_PER_TX env var to adjust."
            )

        # Rolling 24h window
        now = time.time()
        cutoff = now - 86400
        self._spend_log = [(t, a) for t, a in self._spend_log if t > cutoff]
        spent_today = sum(a for _, a in self._spend_log)

        if spent_today + amount > MAX_SEND_PER_DAY:
            return (
                f"Daily limit would be exceeded: {spent_today:.2f} already spent + {amount} = "
                f"{spent_today + amount:.2f} USDC (limit: {MAX_SEND_PER_DAY} USDC/day). "
                f"Set WALLET_MAX_PER_DAY env var to adjust."
            )

        return ""

    async def _request_send(self, chain: str, to: str, amount: float) -> ToolResult:
        """Request a transfer — creates pending approval and notifies owner via Telegram."""
        if not to:
            return ToolResult(success=False, error="Missing 'to' address")
        if not amount or amount <= 0:
            return ToolResult(success=False, error="Amount must be positive")

        # Enforce spending limits
        limit_error = self._check_spending_limits(to, amount)
        if limit_error:
            logger.warning(f"Wallet send blocked: {limit_error}")
            return ToolResult(success=False, error=limit_error)

        private_key = self._keystore.get_private_key(chain)
        if not private_key:
            return ToolResult(success=False, error=f"No {chain} wallet. Generate one first.")

        # Clean expired pending transfers
        now = time.time()
        self._pending_transfers = {
            code: info for code, info in self._pending_transfers.items()
            if now - info["created_at"] < APPROVAL_EXPIRY_SECONDS
        }

        # Generate 6-digit approval code
        code = secrets.token_hex(3).upper()  # e.g., "A3F1B2"

        self._pending_transfers[code] = {
            "chain": chain,
            "to": to,
            "amount": amount,
            "created_at": now,
        }

        logger.info(f"Wallet transfer pending approval: {amount} USDC on {chain} to {to} (code: {code})")

        # Notify owner via Telegram
        await self._notify_owner_approval(chain, to, amount, code)

        return ToolResult(
            success=True,
            output=(
                f"Transfer requires owner approval.\n"
                f"Requested: {amount} USDC on {chain} to {to}\n"
                f"Approval code sent to owner via Telegram.\n"
                f"Owner must reply with the code to confirm (expires in 5 minutes)."
            ),
            metadata={"pending": True, "chain": chain, "to": to, "amount": amount},
        )

    async def _confirm_send(self, approval_code: str) -> ToolResult:
        """Execute a previously approved transfer using the approval code."""
        if not approval_code:
            return ToolResult(success=False, error="Missing 'approval_code'")

        code = approval_code.strip().upper()

        # Look up pending transfer
        info = self._pending_transfers.get(code)
        if not info:
            return ToolResult(
                success=False,
                error="Invalid or expired approval code. Request a new transfer.",
            )

        # Check expiry
        if time.time() - info["created_at"] > APPROVAL_EXPIRY_SECONDS:
            del self._pending_transfers[code]
            return ToolResult(success=False, error="Approval code expired. Request a new transfer.")

        chain = info["chain"]
        to = info["to"]
        amount = info["amount"]

        # Remove from pending (one-time use)
        del self._pending_transfers[code]

        # Execute the transfer
        private_key = self._keystore.get_private_key(chain)
        if not private_key:
            return ToolResult(success=False, error=f"No {chain} wallet.")

        adapter = self._chain_adapter(chain)
        tx_hash = await adapter.send_usdc(private_key, to, amount)

        # Record spend + ledger
        self._spend_log.append((time.time(), amount))
        from_addr = self._keystore.get_address(chain) or "unknown"
        self._ledger.record(chain, "send", from_addr, to, amount, tx_hash=tx_hash)
        logger.info(f"Wallet APPROVED send: {amount} USDC on {chain} to {to} (tx: {tx_hash})")

        return ToolResult(
            success=True,
            output=f"Transfer approved and executed.\nSent {amount} USDC on {chain} to {to}\nTransaction: {tx_hash}",
            metadata={"chain": chain, "tx_hash": tx_hash, "amount": amount, "to": to},
        )

    async def _sweep(self, chain: str) -> ToolResult:
        """Sweep USDC above threshold to owner's personal wallet."""
        owner_addr = OWNER_BASE_ADDRESS if chain == "base" else OWNER_SOLANA_ADDRESS
        if not owner_addr:
            return ToolResult(
                success=False,
                error=f"No owner {chain} address configured. Set OWNER_{chain.upper()}_ADDRESS env var.",
            )

        address = self._keystore.get_address(chain)
        if not address:
            return ToolResult(success=False, error=f"No {chain} wallet. Generate one first.")

        # Check current USDC balance
        adapter = self._chain_adapter(chain)
        balances = await adapter.get_balance(address)
        usdc_balance = float(balances.get("usdc", 0))

        if usdc_balance <= SWEEP_THRESHOLD:
            return ToolResult(
                success=True,
                output=(
                    f"No sweep needed. {chain} USDC balance: {usdc_balance:.2f}\n"
                    f"Threshold: {SWEEP_THRESHOLD} USDC"
                ),
                metadata={"chain": chain, "balance": usdc_balance, "swept": False},
            )

        # Sweep: send everything above SWEEP_KEEP to owner
        sweep_amount = usdc_balance - SWEEP_KEEP
        if sweep_amount <= 0:
            return ToolResult(
                success=True,
                output=f"Balance {usdc_balance:.2f} USDC is at or below keep amount ({SWEEP_KEEP}).",
                metadata={"chain": chain, "balance": usdc_balance, "swept": False},
            )

        private_key = self._keystore.get_private_key(chain)
        if not private_key:
            return ToolResult(success=False, error=f"No {chain} wallet key.")

        tx_hash = await adapter.send_usdc(private_key, owner_addr, sweep_amount)
        self._ledger.record(chain, "sweep", address, owner_addr, sweep_amount, tx_hash=tx_hash, note="auto-sweep to owner")
        logger.info(
            f"Wallet SWEEP: {sweep_amount:.2f} USDC on {chain} → owner ({owner_addr}) tx: {tx_hash}"
        )

        return ToolResult(
            success=True,
            output=(
                f"Swept {sweep_amount:.2f} USDC on {chain} to owner wallet.\n"
                f"Kept {SWEEP_KEEP} USDC as buffer.\n"
                f"To: {owner_addr}\n"
                f"Transaction: {tx_hash}"
            ),
            metadata={"chain": chain, "tx_hash": tx_hash, "amount": sweep_amount, "to": owner_addr},
        )

    async def _ledger_query(self, chain: str, limit: int = 10) -> ToolResult:
        """Show recent transaction history."""
        # Pass chain=None to show all chains, or filter by specific chain
        filter_chain = chain if chain in SUPPORTED_CHAINS else None
        entries = self._ledger.get_recent(limit=limit, chain=filter_chain)
        summary = self._ledger.get_summary(chain=filter_chain)

        formatted = self._ledger.format_entries(entries)
        chain_label = chain if filter_chain else "all chains"

        output = (
            f"Transaction Ledger ({chain_label}) — last {limit}:\n\n"
            f"{formatted}\n\n"
            f"Summary: {summary['total_transactions']} txns | "
            f"Sent: {summary['total_sent']} USDC | "
            f"Received: {summary['total_received']} USDC | "
            f"Net: {summary['net']} USDC"
        )

        return ToolResult(success=True, output=output, metadata=summary)

    async def _notify_owner_approval(self, chain: str, to: str, amount: float, code: str):
        """Send approval request to owner via Telegram."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not bot_token or not chat_id:
            logger.warning("Cannot send approval request — TELEGRAM_BOT_TOKEN/CHAT_ID not set")
            return

        message = (
            f"💰 WALLET TRANSFER APPROVAL REQUIRED\n\n"
            f"Amount: {amount} USDC\n"
            f"Chain: {chain}\n"
            f"To: {to}\n\n"
            f"Approval code: {code}\n\n"
            f"To approve, reply: approve {code}\n"
            f"Expires in 5 minutes. Ignore to deny."
        )

        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "chat_id": chat_id,
                    "text": message,
                })
            logger.info(f"Approval request sent to Telegram for {amount} USDC transfer")
        except Exception as e:
            logger.error(f"Failed to send Telegram approval request: {e}")

    async def _sign(self, chain: str, message: str) -> ToolResult:
        if not message:
            return ToolResult(success=False, error="Missing 'message' to sign")

        private_key = self._keystore.get_private_key(chain)
        if not private_key:
            return ToolResult(success=False, error=f"No {chain} wallet. Generate one first.")

        adapter = self._chain_adapter(chain)
        signature = await adapter.sign_message(private_key, message)

        return ToolResult(
            success=True,
            output=f"Signed message on {chain}",
            metadata={"chain": chain, "signature": signature},
        )

    async def _tx_status(self, chain: str, tx_hash: str) -> ToolResult:
        if not tx_hash:
            return ToolResult(success=False, error="Missing 'tx_hash'")

        if chain == "base":
            status = await self._base.get_tx_status(tx_hash)
            return ToolResult(
                success=True,
                output=f"Transaction {tx_hash}: {status['status']} (block {status.get('block', 'N/A')})",
                metadata=status,
            )
        else:
            return ToolResult(success=False, error="tx_status not yet supported for Solana")
