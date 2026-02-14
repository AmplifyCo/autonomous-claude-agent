#!/usr/bin/env python3
"""Export coreBrain snapshot for EC2 continuity."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.brain.core_brain import CoreBrain


def main():
    """Export coreBrain to snapshot file."""
    print("📦 Exporting coreBrain snapshot...")

    try:
        brain = CoreBrain()
        snapshot_path = brain.export_snapshot()

        print(f"✅ Exported to: {snapshot_path}")
        print("\nNext steps:")
        print("1. git add data/core_brain_snapshot.json")
        print("2. git commit -m 'chore: Add coreBrain snapshot for EC2 continuity'")
        print("3. git push")
        print("\nEC2 will auto-import this snapshot on startup.")

    except Exception as e:
        print(f"❌ Error exporting coreBrain: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
