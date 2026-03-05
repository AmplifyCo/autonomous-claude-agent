"""Base L2 (EVM) chain adapter — ETH + USDC balance, transfers, signing."""

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

# Standard USDC ERC-20 ABI (transfer + balanceOf + decimals)
USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


class BaseChain:
    """Base L2 chain adapter using web3.py."""

    USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    USDC_DECIMALS = 6
    CHAIN_ID = 8453  # Base mainnet

    def __init__(self):
        self.rpc_url = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

    def _get_web3(self):
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not w3.is_connected():
            raise ConnectionError(f"Cannot connect to Base RPC: {self.rpc_url}")
        return w3

    async def get_balance(self, address: str) -> Dict[str, str]:
        """Get ETH and USDC balance.

        Returns:
            {"eth": "0.01234", "usdc": "150.00"}
        """
        w3 = self._get_web3()
        address = w3.to_checksum_address(address)

        # ETH balance
        eth_wei = w3.eth.get_balance(address)
        eth = w3.from_wei(eth_wei, "ether")

        # USDC balance
        usdc_contract = w3.eth.contract(
            address=w3.to_checksum_address(self.USDC_ADDRESS),
            abi=USDC_ABI,
        )
        usdc_raw = usdc_contract.functions.balanceOf(address).call()
        usdc = usdc_raw / (10 ** self.USDC_DECIMALS)

        return {
            "eth": f"{eth:.6f}",
            "usdc": f"{usdc:.2f}",
        }

    async def send_usdc(self, private_key: str, to: str, amount: float) -> str:
        """Send USDC to an address.

        Args:
            private_key: Hex private key
            to: Recipient address
            amount: USDC amount (e.g., 10.50)

        Returns:
            Transaction hash hex string
        """
        from eth_account import Account

        w3 = self._get_web3()
        account = Account.from_key(private_key)
        to = w3.to_checksum_address(to)

        usdc_contract = w3.eth.contract(
            address=w3.to_checksum_address(self.USDC_ADDRESS),
            abi=USDC_ABI,
        )

        # Convert amount to smallest unit
        amount_raw = int(amount * (10 ** self.USDC_DECIMALS))

        # Build transaction
        tx = usdc_contract.functions.transfer(to, amount_raw).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 100_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": self.CHAIN_ID,
        })

        # Sign and send
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        hex_hash = tx_hash.hex()

        logger.info(f"Base USDC transfer sent: {hex_hash}")
        return hex_hash

    async def sign_message(self, private_key: str, message: str) -> str:
        """EIP-191 personal sign.

        Args:
            private_key: Hex private key
            message: Message to sign

        Returns:
            Hex signature
        """
        from eth_account import Account
        from eth_account.messages import encode_defunct

        msg = encode_defunct(text=message)
        signed = Account.sign_message(msg, private_key)
        return signed.signature.hex()

    async def get_tx_status(self, tx_hash: str) -> Dict[str, str]:
        """Check transaction status.

        Returns:
            {"status": "confirmed"|"pending"|"failed", "block": "12345"}
        """
        w3 = self._get_web3()
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                return {"status": "pending", "block": ""}
            status = "confirmed" if receipt["status"] == 1 else "failed"
            return {"status": status, "block": str(receipt["blockNumber"])}
        except Exception:
            return {"status": "pending", "block": ""}
