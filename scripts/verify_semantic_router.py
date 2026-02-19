"""Verification script for Semantic Router."""
import asyncio
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.brain.semantic_router import SemanticRouter

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_semantic_router():
    """Test Semantic Router functionality."""
    
    print("\n🧪 Testing Semantic Router...")
    print("="*50)
    
    # Initialize router with test paths
    router = SemanticRouter(
        db_path="data/chroma_test_semantic",
        golden_intents_path="data/golden_intents.json"
    )
    
    await router.initialize()
    
    test_cases = [
        # Exact match
        ("check email", "action", "email_list"),
        
        # Semantic match (different phrasing)
        ("show me my unread emails", "action", "email_list"),
        ("what is the system uptime", "status", None),
        ("pull latest code from git", "git_update", None),
        
        # No match (should return None)
        ("What is the meaning of life?", None, None),
        ("Compose a poem about rust", None, None),
    ]
    
    passes = 0
    fails = 0
    
    for message, expected_action, expected_tool in test_cases:
        print(f"\nMessage: '{message}'")
        result = await router.route(message)
        
        if result:
            action = result["action"]
            confidence = result["confidence"]
            tools = result.get("tool_hints", [])
            print(f"  -> Match: {action} ({confidence:.2f}) Tools: {tools}")
            
            if expected_action:
                if action == expected_action:
                    # Check tool hint if expected
                    if expected_tool:
                        if expected_tool in tools:
                            print("  ✅ PASS")
                            passes += 1
                        else:
                            print(f"  ❌ FAIL (Wrong tool: expected {expected_tool}, got {tools})")
                            fails += 1
                    else:
                        print("  ✅ PASS")
                        passes += 1
                else:
                    print(f"  ❌ FAIL (Expected {expected_action}, got {action})")
                    fails += 1
            else:
                 print(f"  ❌ FAIL (Expected None, got match)")
                 fails += 1
        else:
            print("  -> No match")
            if expected_action is None:
                print("  ✅ PASS")
                passes += 1
            else:
                print(f"  ❌ FAIL (Expected {expected_action}, got None)")
                fails += 1

    print("="*50)
    print(f"Results: {passes} PASS, {fails} FAIL")
    
    # Cleanup
    import shutil
    if Path("data/chroma_test_semantic").exists():
        shutil.rmtree("data/chroma_test_semantic")

if __name__ == "__main__":
    asyncio.run(test_semantic_router())
