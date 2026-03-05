"""Solana chain adapter — SOL + USDC balance, transfers, signing."""

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)


class SolanaChain:
    """Solana chain adapter using solana-py + solders."""

    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    USDC_DECIMALS = 6

    def __init__(self):
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    def _get_client(self):
        from solana.rpc.api import Client
        return Client(self.rpc_url)

    async def get_balance(self, address: str) -> Dict[str, str]:
        """Get SOL and USDC balance.

        Returns:
            {"sol": "0.50000", "usdc": "100.00"}
        """
        from solders.pubkey import Pubkey  # type: ignore
        from solana.rpc.types import TokenAccountOpts

        client = self._get_client()
        pubkey = Pubkey.from_string(address)

        # SOL balance
        sol_resp = client.get_balance(pubkey)
        sol_lamports = sol_resp.value
        sol = sol_lamports / 1_000_000_000

        # USDC (SPL token) balance
        usdc = 0.0
        try:
            usdc_mint = Pubkey.from_string(self.USDC_MINT)
            token_resp = client.get_token_accounts_by_owner(
                pubkey,
                TokenAccountOpts(mint=usdc_mint),
            )
            if token_resp.value:
                # Parse token account data for balance
                for account in token_resp.value:
                    info = client.get_token_account_balance(account.pubkey)
                    if info.value:
                        usdc = float(info.value.ui_amount or 0)
                        break
        except Exception as e:
            logger.warning(f"Failed to fetch Solana USDC balance: {e}")

        return {
            "sol": f"{sol:.5f}",
            "usdc": f"{usdc:.2f}",
        }

    async def send_usdc(self, private_key: str, to: str, amount: float) -> str:
        """Send USDC (SPL token) to an address.

        Args:
            private_key: Base58-encoded private key
            to: Recipient address (base58)
            amount: USDC amount

        Returns:
            Transaction signature (base58)
        """
        import base58
        from solders.keypair import Keypair  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore
        from solders.system_program import TransferParams, transfer
        from spl.token.instructions import (  # type: ignore
            TransferCheckedParams,
            transfer_checked,
        )
        from spl.token.constants import TOKEN_PROGRAM_ID  # type: ignore
        from solana.transaction import Transaction
        from solana.rpc.types import TokenAccountOpts

        client = self._get_client()

        # Reconstruct keypair from base58 private key
        key_bytes = base58.b58decode(private_key)
        sender_kp = Keypair.from_bytes(key_bytes)
        sender_pubkey = sender_kp.pubkey()

        to_pubkey = Pubkey.from_string(to)
        usdc_mint = Pubkey.from_string(self.USDC_MINT)

        # Find sender's USDC token account
        sender_token_resp = client.get_token_accounts_by_owner(
            sender_pubkey, TokenAccountOpts(mint=usdc_mint)
        )
        if not sender_token_resp.value:
            raise RuntimeError("No USDC token account found for sender")
        sender_token_account = sender_token_resp.value[0].pubkey

        # Find or derive recipient's USDC token account
        recipient_token_resp = client.get_token_accounts_by_owner(
            to_pubkey, TokenAccountOpts(mint=usdc_mint)
        )
        if not recipient_token_resp.value:
            raise RuntimeError(
                "Recipient has no USDC token account. "
                "They need to create one first (or use associated token account)."
            )
        recipient_token_account = recipient_token_resp.value[0].pubkey

        # Build transfer instruction
        amount_raw = int(amount * (10 ** self.USDC_DECIMALS))
        ix = transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=sender_token_account,
                mint=usdc_mint,
                dest=recipient_token_account,
                owner=sender_pubkey,
                amount=amount_raw,
                decimals=self.USDC_DECIMALS,
            )
        )

        # Send transaction
        blockhash_resp = client.get_latest_blockhash()
        recent_blockhash = blockhash_resp.value.blockhash

        tx = Transaction(recent_blockhash=recent_blockhash)
        tx.add(ix)
        tx.sign(sender_kp)

        result = client.send_transaction(tx, sender_kp)
        sig = str(result.value)

        logger.info(f"Solana USDC transfer sent: {sig}")
        return sig

    async def sign_message(self, private_key: str, message: str) -> str:
        """Ed25519 sign a message.

        Args:
            private_key: Base58-encoded private key
            message: Message to sign

        Returns:
            Base58-encoded signature
        """
        import base58
        from solders.keypair import Keypair  # type: ignore

        key_bytes = base58.b58decode(private_key)
        kp = Keypair.from_bytes(key_bytes)
        sig = kp.sign_message(message.encode())
        return base58.b58encode(bytes(sig)).decode()
