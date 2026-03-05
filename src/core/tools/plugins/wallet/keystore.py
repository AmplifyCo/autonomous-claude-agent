"""Encrypted keystore for wallet private keys.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
Keys stored at data/wallet_keystore.enc with 0o600 permissions.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class WalletKeystore:
    """Encrypted storage for blockchain private keys."""

    def __init__(self, encryption_key: str, path: str = ""):
        """Initialize keystore.

        Args:
            encryption_key: Fernet encryption key (base64-encoded 32 bytes).
                            Generate with: Fernet.generate_key().decode()
            path: Path to encrypted keystore file. Defaults to data/wallet_keystore.enc
        """
        self.fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        self.path = Path(path) if path else Path("data/wallet_keystore.enc")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[dict] = None

    def _load(self) -> dict:
        """Load and decrypt keystore from disk."""
        if self._cache is not None:
            return self._cache

        if not self.path.exists():
            self._cache = {}
            return self._cache

        try:
            encrypted = self.path.read_bytes()
            decrypted = self.fernet.decrypt(encrypted)
            self._cache = json.loads(decrypted.decode())
            return self._cache
        except Exception as e:
            logger.error(f"Failed to load keystore: {e}")
            raise RuntimeError("Keystore decryption failed — check WALLET_ENCRYPTION_KEY") from e

    def _save(self, data: dict):
        """Encrypt and save keystore to disk."""
        raw = json.dumps(data).encode()
        encrypted = self.fernet.encrypt(raw)
        self.path.write_bytes(encrypted)
        # Restrict permissions to owner only
        os.chmod(self.path, 0o600)
        self._cache = data

    def generate_keypair(self, chain: str) -> str:
        """Generate a new keypair for the given chain.

        Args:
            chain: "base" or "solana"

        Returns:
            Public address of the generated keypair
        """
        data = self._load()
        if chain in data:
            return data[chain]["address"]

        if chain == "base":
            address, private_key = self._generate_evm_keypair()
        elif chain == "solana":
            address, private_key = self._generate_solana_keypair()
        else:
            raise ValueError(f"Unsupported chain: {chain}")

        data[chain] = {
            "address": address,
            "private_key": private_key,
        }
        self._save(data)
        logger.info(f"Generated {chain} keypair: {address}")
        return address

    def get_address(self, chain: str) -> Optional[str]:
        """Get public address for chain."""
        data = self._load()
        entry = data.get(chain)
        return entry["address"] if entry else None

    def get_private_key(self, chain: str) -> Optional[str]:
        """Get decrypted private key. Internal use only — never expose in output."""
        data = self._load()
        entry = data.get(chain)
        return entry["private_key"] if entry else None

    def has_chain(self, chain: str) -> bool:
        """Check if keypair exists for chain."""
        data = self._load()
        return chain in data

    def list_chains(self) -> list:
        """List all chains with stored keypairs."""
        data = self._load()
        return list(data.keys())

    def _generate_evm_keypair(self) -> tuple:
        """Generate an Ethereum/Base keypair.

        Returns:
            (address, private_key_hex)
        """
        from eth_account import Account
        account = Account.create()
        return account.address, account.key.hex()

    def _generate_solana_keypair(self) -> tuple:
        """Generate a Solana keypair.

        Returns:
            (address_base58, private_key_base58)
        """
        from solders.keypair import Keypair  # type: ignore
        import base58

        kp = Keypair()
        address = str(kp.pubkey())
        private_key = base58.b58encode(bytes(kp)).decode()
        return address, private_key
