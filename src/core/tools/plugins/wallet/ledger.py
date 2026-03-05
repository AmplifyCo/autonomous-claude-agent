"""Wallet transaction ledger — append-only JSONL log of all transactions."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

LEDGER_PATH = os.getenv("WALLET_LEDGER_PATH", "data/wallet_ledger.jsonl")


class WalletLedger:
    """Append-only transaction ledger stored as JSONL."""

    def __init__(self, path: str = LEDGER_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        chain: str,
        tx_type: str,
        from_addr: str,
        to_addr: str,
        amount: float,
        token: str = "USDC",
        tx_hash: str = "",
        status: str = "confirmed",
        note: str = "",
    ):
        """Append a transaction record to the ledger."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "epoch": time.time(),
            "chain": chain,
            "type": tx_type,
            "from": from_addr,
            "to": to_addr,
            "amount": amount,
            "token": token,
            "tx_hash": tx_hash,
            "status": status,
        }
        if note:
            entry["note"] = note

        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            logger.info(f"Ledger: recorded {tx_type} {amount} {token} on {chain}")
        except Exception as e:
            logger.error(f"Ledger write failed: {e}")

    def get_recent(self, limit: int = 10, chain: Optional[str] = None) -> List[Dict]:
        """Read the most recent transactions, optionally filtered by chain."""
        if not self.path.exists():
            return []

        entries = []
        try:
            with open(self.path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if chain and entry.get("chain") != chain:
                            continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.error(f"Ledger read failed: {e}")
            return []

        return entries[-limit:]

    def get_summary(self, chain: Optional[str] = None) -> Dict:
        """Get a summary of all transactions."""
        entries = self.get_recent(limit=999999, chain=chain)

        total_sent = sum(e["amount"] for e in entries if e["type"] in ("send", "sweep"))
        total_received = sum(e["amount"] for e in entries if e["type"] == "receive")
        tx_count = len(entries)

        return {
            "total_transactions": tx_count,
            "total_sent": round(total_sent, 2),
            "total_received": round(total_received, 2),
            "net": round(total_received - total_sent, 2),
        }

    def format_entries(self, entries: List[Dict]) -> str:
        """Format ledger entries for display."""
        if not entries:
            return "No transactions recorded."

        lines = []
        for e in entries:
            ts = e.get("timestamp", "")[:19].replace("T", " ")
            direction = "→" if e["type"] in ("send", "sweep") else "←"
            addr = e["to"] if direction == "→" else e["from"]
            short_addr = addr[:6] + "..." + addr[-4:] if len(addr) > 12 else addr
            tx = e.get("tx_hash", "")
            short_tx = tx[:8] + "..." if tx else "N/A"
            note = f" ({e['note']})" if e.get("note") else ""

            lines.append(
                f"{ts} | {e['chain']:6s} | {e['type']:7s} | "
                f"{direction} {e['amount']:>10.2f} {e.get('token', 'USDC')} | "
                f"{short_addr} | tx:{short_tx}{note}"
            )

        return "\n".join(lines)
