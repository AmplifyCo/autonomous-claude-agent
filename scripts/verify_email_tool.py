import sys
import os

# Add repo root to path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

try:
    from src.core.tools.email import EmailTool
    print("EmailTool imported successfully")
    
    tool = EmailTool("imap", "smtp", "user", "pass")
    print(f"Tool name: {tool.name}")
    print(f"Operations: {tool.parameters['operation']['enum']}")
    
    expected_ops = ["delete_email", "mark_unread"]
    for op in expected_ops:
        if op in tool.parameters['operation']['enum']:
            print(f"Verified operation: {op}")
        else:
            print(f"MISSING operation: {op}")
            sys.exit(1)
            
    print("Verification complete")
except Exception as e:
    print(f"Verification failed: {e}")
    sys.exit(1)
