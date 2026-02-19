import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Mock twilio, aiohttp before importing modules that use it
sys.modules["twilio"] = MagicMock()
sys.modules["twilio.rest"] = MagicMock()
sys.modules["aiohttp"] = MagicMock()

# Add repo root to path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

from src.channels.whatsapp_channel import WhatsAppChannel
from src.core.tools.whatsapp import WhatsAppTool

async def test_whatsapp_channel_meta():
    print("\n📱 Testing WhatsApp Channel (Meta API)...")
    
    # Mock conversation manager
    mock_cm = AsyncMock()
    mock_cm.process_message.return_value = "Hello from AI!"
    
    # Initialize channel
    channel = WhatsAppChannel(
        api_token="mock_token",
        phone_id="12345",
        verify_token="test_verify",
        conversation_manager=mock_cm
    )
    
    # 1. Test Verification (GET)
    print("1. Testing Webhook Verification...")
    verify_params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "test_verify",
        "hub.challenge": "123456789"
    }
    challenge = await channel.verify_webhook(verify_params)
    if challenge == "123456789":
        print("✅ Verification Successful")
    else:
        print(f"❌ Verification Failed: {challenge}")

    # 2. Test Incoming Message (POST)
    print("2. Testing Incoming Message...")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "12345", "phone_number_id": "12345"},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": "16505551234"}],
                    "messages": [{
                        "from": "16505551234",
                        "id": "wamid.HBgLM...",
                        "timestamp": "167...",
                        "text": {"body": "Hello AI"},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    # Mock send_message to avoid network call
    channel.send_message = AsyncMock()

    result = await channel.handle_webhook_payload(payload)
    
    if result.get("status") == "received":
        print("✅ Webhook processed successfully")
    else:
        print(f"❌ Webhook failed: {result}")
        
    await asyncio.sleep(0.1) # Wait for background task
    mock_cm.process_message.assert_called_once()
    print("✅ ConversationManager called")
    channel.send_message.assert_called_once()
    print("✅ Response sent (mocked)")

async def test_whatsapp_tool_meta():
    print("\n🛠️ Testing WhatsApp Tool (Meta API)...")
    
    tool = WhatsAppTool(api_token="mock_token", phone_id="12345")
    
    # Configure aiohttp mock (global mock)
    import aiohttp
    
    # Mock response
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json.return_value = {"messages": [{"id": "wamid.123"}]}
    
    # Mock post context manager
    mock_post_ctx = AsyncMock()
    mock_post_ctx.__aenter__.return_value = mock_response
    mock_post_ctx.__aexit__.return_value = None
    
    # Mock session
    mock_session = AsyncMock()
    mock_session.post.return_value = mock_post_ctx
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    
    # Configure ClientSession to return our mock session
    aiohttp.ClientSession.return_value = mock_session
    
    # Execute tool
    print("Executing whatsapp_send...")
    result = await tool.execute(to="16505551234", body="Outbound test")
    
    if result["success"]:
        print(f"✅ Tool execution successful: {result['output']}")
    else:
        print(f"❌ Tool execution failed: {result.get('error')}")

    # Verify API call
    mock_session.post.assert_called_once()
    args, kwargs = mock_session.post.call_args
    assert kwargs["json"]["to"] == "16505551234"
    print("✅ Meta API called with correct parameters")

if __name__ == "__main__":
    asyncio.run(test_whatsapp_channel_meta())
    asyncio.run(test_whatsapp_tool_meta())
