"""ERC-8004 on-chain agent identity — register, resolve, verify.

ERC-8004 defines an NFT-based identity standard for AI agents:
- Register a human-readable handle (e.g., "nova.agent")
- Resolve handles to Ethereum addresses + metadata
- Verify agent identity on-chain

Registry contract lives on Base (EVM-compatible L2).
"""

import json
import logging
import os
from typing import Optional

from src.core.tools.base import BaseTool
from src.core.types import ToolResult

from ..wallet.keystore import WalletKeystore
from ..wallet.chains.base_chain import BaseChain

logger = logging.getLogger(__name__)

# Minimal ERC-8004 registry ABI (register, resolve, verify)
# Full ABI would come from the deployed contract — this covers core methods
REGISTRY_ABI = [
    {
        "inputs": [
            {"name": "handle", "type": "string"},
            {"name": "metadata", "type": "string"},
        ],
        "name": "register",
        "outputs": [{"name": "tokenId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "handle", "type": "string"}],
        "name": "resolve",
        "outputs": [
            {"name": "owner", "type": "address"},
            {"name": "metadata", "type": "string"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "addr", "type": "address"}],
        "name": "getIdentity",
        "outputs": [
            {"name": "handle", "type": "string"},
            {"name": "metadata", "type": "string"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "addr", "type": "address"}],
        "name": "isRegistered",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ERC8004Tool(BaseTool):
    """ERC-8004 agent identity — register handle, resolve, verify."""

    name = "erc8004"
    description = (
        "On-chain agent identity (ERC-8004). Operations: "
        "'register' mints an identity NFT with a handle, "
        "'resolve' looks up a handle to find the owner address, "
        "'verify' checks if an address has a registered identity, "
        "'my_identity' shows Nova's own registered identity."
    )
    parameters = {
        "operation": {
            "type": "string",
            "description": "Identity operation",
            "enum": ["register", "resolve", "verify", "my_identity"],
        },
        "handle": {
            "type": "string",
            "description": "Agent handle (e.g., 'nova.agent') — for register/resolve",
        },
        "address": {
            "type": "string",
            "description": "Ethereum address — for verify",
        },
        "metadata": {
            "type": "string",
            "description": "JSON metadata for registration (name, description, capabilities)",
        },
    }

    def __init__(self, encryption_key: str = "", registry_address: str = ""):
        self._registry_address = registry_address or os.getenv("ERC8004_REGISTRY_ADDRESS", "")
        if not encryption_key:
            logger.warning("erc8004 plugin: no WALLET_ENCRYPTION_KEY — identity ops disabled")
            self._keystore = None
        else:
            self._keystore = WalletKeystore(encryption_key)
        self._base_chain = BaseChain()

    def _get_contract(self):
        """Get the registry contract instance."""
        if not self._registry_address:
            raise RuntimeError(
                "ERC-8004 registry address not configured. "
                "Set ERC8004_REGISTRY_ADDRESS env var."
            )
        w3 = self._base_chain._get_web3()
        return w3.eth.contract(
            address=w3.to_checksum_address(self._registry_address),
            abi=REGISTRY_ABI,
        )

    async def execute(self, operation: str = "", **kwargs) -> ToolResult:
        try:
            if operation == "register":
                return await self._register(
                    kwargs.get("handle", ""),
                    kwargs.get("metadata", ""),
                )
            elif operation == "resolve":
                return await self._resolve(kwargs.get("handle", ""))
            elif operation == "verify":
                return await self._verify(kwargs.get("address", ""))
            elif operation == "my_identity":
                return await self._my_identity()
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"ERC-8004 {operation} failed: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    async def _register(self, handle: str, metadata_str: str) -> ToolResult:
        """Register an on-chain agent identity (mints ERC-8004 NFT)."""
        if not handle:
            return ToolResult(success=False, error="Missing 'handle' for registration")
        if not self._keystore:
            return ToolResult(success=False, error="Wallet not configured")

        private_key = self._keystore.get_private_key("base")
        if not private_key:
            return ToolResult(
                success=False,
                error="No Base wallet. Generate one first with the wallet tool.",
            )

        from eth_account import Account

        w3 = self._base_chain._get_web3()
        account = Account.from_key(private_key)
        contract = self._get_contract()

        # Build metadata JSON
        if not metadata_str:
            bot_name = os.getenv("BOT_NAME", "Nova")
            owner_name = os.getenv("OWNER_NAME", "User")
            metadata_str = json.dumps({
                "name": bot_name,
                "type": "ai_agent",
                "owner": owner_name,
                "description": f"{bot_name} — autonomous AI assistant",
            })

        # Build and send registration transaction
        tx = contract.functions.register(handle, metadata_str).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 300_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": self._base_chain.CHAIN_ID,
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        hex_hash = tx_hash.hex()

        logger.info(f"ERC-8004 registration tx: {hex_hash} for handle: {handle}")
        return ToolResult(
            success=True,
            output=f"Identity registration submitted for '{handle}'\nTransaction: {hex_hash}",
            metadata={"tx_hash": hex_hash, "handle": handle},
        )

    async def _resolve(self, handle: str) -> ToolResult:
        """Resolve a handle to its owner address and metadata."""
        if not handle:
            return ToolResult(success=False, error="Missing 'handle' to resolve")

        contract = self._get_contract()
        try:
            owner, metadata_str, token_id = contract.functions.resolve(handle).call()
        except Exception as e:
            return ToolResult(success=False, error=f"Handle '{handle}' not found: {e}")

        # Parse metadata
        try:
            metadata = json.loads(metadata_str)
        except (json.JSONDecodeError, TypeError):
            metadata = {"raw": metadata_str}

        return ToolResult(
            success=True,
            output=(
                f"Handle: {handle}\n"
                f"Owner: {owner}\n"
                f"Token ID: {token_id}\n"
                f"Metadata: {json.dumps(metadata, indent=2)}"
            ),
            metadata={"handle": handle, "owner": owner, "token_id": token_id, **metadata},
        )

    async def _verify(self, address: str) -> ToolResult:
        """Check if an address has a registered ERC-8004 identity."""
        if not address:
            return ToolResult(success=False, error="Missing 'address' to verify")

        w3 = self._base_chain._get_web3()
        address = w3.to_checksum_address(address)
        contract = self._get_contract()

        try:
            is_registered = contract.functions.isRegistered(address).call()
        except Exception as e:
            return ToolResult(success=False, error=f"Verification failed: {e}")

        if not is_registered:
            return ToolResult(
                success=True,
                output=f"Address {address} has NO registered agent identity.",
                metadata={"address": address, "registered": False},
            )

        # Get identity details
        try:
            handle, metadata_str, token_id = contract.functions.getIdentity(address).call()
            return ToolResult(
                success=True,
                output=(
                    f"Address {address} is a registered agent.\n"
                    f"Handle: {handle}\n"
                    f"Token ID: {token_id}"
                ),
                metadata={"address": address, "registered": True, "handle": handle, "token_id": token_id},
            )
        except Exception:
            return ToolResult(
                success=True,
                output=f"Address {address} is registered but identity details unavailable.",
                metadata={"address": address, "registered": True},
            )

    async def _my_identity(self) -> ToolResult:
        """Show Nova's own registered identity."""
        if not self._keystore:
            return ToolResult(success=False, error="Wallet not configured")

        address = self._keystore.get_address("base")
        if not address:
            return ToolResult(
                success=False,
                error="No Base wallet. Generate one first with the wallet tool.",
            )

        return await self._verify(address)
