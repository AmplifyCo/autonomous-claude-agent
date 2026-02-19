
import asyncio
import os
import sys

# Add src to path just in case, though we just need litellm
sys.path.insert(0, os.getcwd())

try:
    import litellm
    from litellm import acompletion
    # litellm.set_verbose = True  # specific log level set by user env var `LITELLM_LOG`
except ImportError:
    print("Please install litellm first: pip install litellm")
    sys.exit(1)

async def check_version():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set in environment.")
        return

    # Test 1: Connectivity check with Flash
    print("\n--- Test 1: Connectivity (Gemini Flash) ---")
    try:
        response = await acompletion(
            model="gemini/gemini-2.0-flash",
            messages=[{"role": "user", "content": "Hi"}]
        )
        print("Success! Connection works.")
    except Exception as e:
        print(f"Connectivity failed: {e}")
        return

    # Test 2: Alias check
    print("\n--- Test 2: Alias Resolution (gemini-pro-latest) ---")
    try:
        response = await asyncio.wait_for(acompletion(
            model="gemini/gemini-pro-latest",
            messages=[{
                "role": "user", 
                "content": "What is your exact model version? Are you Gemini 1.5, 2.0, or 3.1? What is your knowledge cutoff date?"
            }]
        ), timeout=10.0)
        print(f"\nResponse Type: {type(response)}")
        print("\n--- Model Response ---")
        if hasattr(response, 'choices'):
            print(response.choices[0].message.content)
        else:
            print(f"Raw Response: {response}")
            
        print("\n--- Metadata ---")
        if hasattr(response, 'model'):
            print(f"Model ID returned: {response.model}")
        else:
            print("No model ID in response.")
        
    except Exception as e:
        print(f"\nError with gemini-pro-latest: {e}")

    # Test 3: Try explicit 1.5-pro
    print("\n--- Test 3: Explicit gemini-1.5-pro-latest ---")
    try:
        response = await acompletion(
            model="gemini/gemini-1.5-pro-latest",
            messages=[{"role": "user", "content": "Hi"}]
        )
        print(f"Success! Response: {response.choices[0].message.content}")
    except Exception as e:
        print(f"Error with 1.5-pro-latest: {e}")

if __name__ == "__main__":
    asyncio.run(check_version())
