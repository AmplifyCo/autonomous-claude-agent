"""Wallet plugin — multi-chain crypto wallet for Base and Solana USDC."""

import logging

from src.core.tools.base import BaseTool
from src.core.types import ToolResult

from .keystore import WalletKeystore
from .chains.base_chain import BaseChain
from .chains.solana_chain import SolanaChain

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ("base", "solana")


class WalletTool(BaseTool):
    """Multi-chain crypto wallet — check balance, send USDC, sign messages."""

    name = "wallet"
    description = (
        "Crypto wallet for Base and Solana chains. "
        "Operations: generate (create keypair), address (show address), "
        "balance (check ETH/SOL + USDC), send (transfer USDC), "
        "sign (sign a message), tx_status (check transaction)."
    )
    parameters = {
        "operation": {
            "type": "string",
            "description": "Wallet operation to perform",
            "enum": ["generate", "address", "balance", "send", "sign", "tx_status"],
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
    }

    def __init__(self, encryption_key: str = ""):
        if not encryption_key:
            logger.warning("Wallet plugin: no WALLET_ENCRYPTION_KEY — wallet disabled")
            self._keystore = None
        else:
            self._keystore = WalletKeystore(encryption_key)
        self._base = BaseChain()
        self._solana = SolanaChain()

    def _chain_adapter(self, chain: str):
        if chain == "base":
            return self._base
        elif chain == "solana":
            return self._solana
        raise ValueError(f"Unsupported chain: {chain}")

    async def execute(self, operation: str = "", chain: str = "base", **kwargs) -> ToolResult:
        if not self._keystore:
            return ToolResult(success=False, error="Wallet not configured — set WALLET_ENCRYPTION_KEY")

        if chain not in SUPPORTED_CHAINS:
            return ToolResult(success=False, error=f"Unsupported chain: {chain}. Use: {SUPPORTED_CHAINS}")

        try:
            if operation == "generate":
                return await self._generate(chain)
            elif operation == "address":
                return await self._address(chain)
            elif operation == "balance":
                return await self._balance(chain)
            elif operation == "send":
                return await self._send(chain, kwargs.get("to", ""), kwargs.get("amount", 0))
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

        return ToolResult(success=True, output=output, metadata={"chain": chain, **balances})

    async def _send(self, chain: str, to: str, amount: float) -> ToolResult:
        if not to:
            return ToolResult(success=False, error="Missing 'to' address")
        if not amount or amount <= 0:
            return ToolResult(success=False, error="Amount must be positive")

        private_key = self._keystore.get_private_key(chain)
        if not private_key:
            return ToolResult(success=False, error=f"No {chain} wallet. Generate one first.")

        adapter = self._chain_adapter(chain)
        tx_hash = await adapter.send_usdc(private_key, to, amount)

        return ToolResult(
            success=True,
            output=f"Sent {amount} USDC on {chain} to {to}\nTransaction: {tx_hash}",
            metadata={"chain": chain, "tx_hash": tx_hash, "amount": amount, "to": to},
        )

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
