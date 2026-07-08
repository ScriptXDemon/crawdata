"""Test Ollama-compatible API connectivity.

Usage:  .venv\Scripts\python.exe tests\test_ollama_api.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.i3softlab.com")
API_KEY = os.environ.get("OLLAMA_API_KEY", "")
MODEL = os.environ.get("OLLAMA_MODEL", "text-model")

if not API_KEY:
    print("OLLAMA_API_KEY not set — skipping (export it to run this connectivity check).")
    sys.exit(0)

CHAT_ENDPOINT = f"{BASE_URL}/v1/chat/completions"
LIST_ENDPOINT = f"{BASE_URL}/v1/models"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def test_list_models() -> bool:
    """Step 1: List available models."""
    print(f"\n[1/3] Listing models at {LIST_ENDPOINT}...")
    try:
        resp = httpx.get(LIST_ENDPOINT, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(models, list):
                model_ids = [m["id"] for m in models]
                print(f"      OK — {len(models)} model(s): {model_ids[:5]}")
            else:
                print(f"      OK — raw response: {json.dumps(data)[:200]}")
            return True
        print(f"      FAIL — HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as exc:
        print(f"      FAIL — {exc}")
        return False


def test_simple_prompt() -> bool:
    """Step 2: Send a simple prompt."""
    print(f"\n[2/3] Sending prompt to {MODEL}...")
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say hello and tell me what you can do in exactly one sentence."}],
        "temperature": 0.7,
        "max_tokens": 100,
    }
    try:
        t0 = time.perf_counter()
        resp = httpx.post(CHAT_ENDPOINT, headers=HEADERS, json=body, timeout=60)
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {})
            print(f"      OK — {elapsed:.1f}s | tokens={tokens}")
            print(f"      Response: {content[:200]}")
            return True
        print(f"      FAIL — HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as exc:
        print(f"      FAIL — {exc}")
        return False


def test_defence_prompt() -> bool:
    """Step 3: Domain-specific prompt (defence/entity extraction style)."""
    print(f"\n[3/3] Testing defence-domain prompt...")
    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Extract any defence companies, weapon systems, and contract values "
                    "from this text. Return JSON with keys: companies, weapons, values.\n\n"
                    "Tata Advanced Systems signed a $320M contract with Airbus for C-295 "
                    "transport aircraft manufacturing in Vadodara. The WhAP 8x8 armoured "
                    "vehicle completed trials."
                ),
            }
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    try:
        t0 = time.perf_counter()
        resp = httpx.post(CHAT_ENDPOINT, headers=HEADERS, json=body, timeout=60)
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            print(f"      OK — {elapsed:.1f}s")
            print(f"      Response: {content[:300]}")
            return True
        print(f"      FAIL — HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as exc:
        print(f"      FAIL — {exc}")
        return False


def main() -> int:
    print(f"BASE: {BASE_URL}")
    print(f"MODEL: {MODEL}")
    print("=" * 50)

    results = [
        ("List models       ", test_list_models()),
        ("Simple prompt     ", test_simple_prompt()),
        ("Defence prompt    ", test_defence_prompt()),
    ]

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok in results if ok)
    for label, ok in results:
        print(f"  {'OK' if ok else 'FAIL'}  {label}")
    print(f"\n  {passed}/{len(results)} tests passed")

    if passed == 0:
        # Debug: raw curl-style check
        print("\n[DEBUG] Raw connectivity check...")
        try:
            r = httpx.get(BASE_URL, timeout=10)
            print(f"  GET {BASE_URL} -> {r.status_code}")
        except Exception as exc:
            print(f"  GET {BASE_URL} -> {exc}")

        # Try without /v1 prefix (plain Ollama)
        try:
            r = httpx.get(f"{BASE_URL}/api/tags", headers=HEADERS, timeout=10)
            print(f"  GET /api/tags -> {r.status_code} {r.text[:200]}")
        except Exception as exc:
            print(f"  GET /api/tags -> {exc}")

        # Try OpenAI-style /v1/chat/completions directly
        try:
            r = httpx.post(
                f"{BASE_URL}/v1/chat/completions",
                headers={**HEADERS, "Authorization": f"Bearer {API_KEY}"},
                json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                timeout=15,
            )
            print(f"  POST /v1/chat/completions -> {r.status_code} {r.text[:300]}")
        except Exception as exc:
            print(f"  POST /v1/chat/completions -> {exc}")

    return 0 if passed == len(results) else 1


def chat_loop() -> int:
    """Interactive prompt loop — type prompts, get responses. /quit to exit."""
    print(f"\nModel: {MODEL}  |  Type /quit to exit, /clear to reset\n")
    messages: list[dict] = []

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0

        if not user_input:
            continue
        if user_input == "/quit":
            print("Bye.")
            return 0
        if user_input == "/clear":
            messages.clear()
            print("[conversation cleared]\n")
            continue

        messages.append({"role": "user", "content": user_input})
        body = {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
        }
        try:
            t0 = time.perf_counter()
            resp = httpx.post(CHAT_ENDPOINT, headers=HEADERS, json=body, timeout=120)
            elapsed = time.perf_counter() - t0
            if resp.status_code != 200:
                print(f"[HTTP {resp.status_code}] {resp.text[:200]}\n")
                messages.pop()
                continue
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {})
            messages.append({"role": "assistant", "content": content})
            print(f"\n{content}\n")
            print(f"[{elapsed:.1f}s | {tokens.get('total_tokens', '?')} tokens]\n")
        except Exception as exc:
            print(f"[Error] {exc}\n")
            messages.pop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chat":
        sys.exit(chat_loop())
    sys.exit(main())
