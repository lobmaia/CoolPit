"""
Checks that your Gemini API key works.

Usage:
    python test_gemini.py
Reads the key from the GEMINI_API_KEY environment variable and sends one short question.
"""
import os
import sys

MODEL = "gemini-3.8-flash"  # gemini-2.5-flash is no longer available to new users

# 1. Check that the key is in the environment (never print the full key)
key = os.environ.get("GEMINI_API_KEY")
if not key:
    print("❌ GEMINI_API_KEY environment variable not found.")
    print("   If you saved it with setx, fully close and reopen VS Code.")
    sys.exit(1)
print(f"🔑 Key found: {key[:6]}...{key[-4:]} (length {len(key)})")

# 2. Check the library
try:
    from google import genai
except ImportError:
    print("❌ google-genai is not installed. Install it first:")
    print("   python -m pip install google-genai")
    sys.exit(1)

# 3. Make a real call
client = genai.Client(api_key=key)
try:
    response = client.models.generate_content(
        model=MODEL,
        contents="Explain the undercut in Formula 1 in one sentence.",
    )
    print(f"✅ Success! ({MODEL}) Response:")
    print(response.text)
except Exception as e:
    msg = str(e)
    print(f"❌ Call failed: {type(e).__name__}")
    print(f"   {msg[:300]}")
    if "API key not valid" in msg or "PERMISSION_DENIED" in msg or "403" in msg:
        print("👉 The key is invalid. Copy it again from AI Studio and save it (watch for extra spaces).")
    elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        print("👉 The key works, but the free-tier limit was reached. Try again in a moment.")
    elif "404" in msg or "NOT_FOUND" in msg:
        print(f"👉 The key works, but model '{MODEL}' is not available. Available flash models:")
        for m in client.models.list():
            if "flash" in m.name:
                print("   -", m.name)
        print("   Change MODEL at the top of this file to one of the names above.")
    sys.exit(1)
