#!/usr/bin/env python3
"""
Standalone Ollama test — analyse an image and save results to JSON.

Sends the image directly to the local moondream vision model via Ollama's
``images`` field (base64-encoded).  Also extracts QR/barcode data with
pyzbar for supplementary text context.

Usage:
  python3 test_ollama.py <image_path>                   # output: <image_path>.ollama.json
  python3 test_ollama.py <image_path> -o result.json    # custom output path
  python3 test_ollama.py <image_path> --timeout 30      # custom timeout (default 120s)
"""

import argparse
import base64
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Error: requests is required.  pip install requests")

try:
    from pyzbar import pyzbar
    from PIL import Image
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434"
MODEL = "moondream"


def scan_image(path):
    """Extract QR/barcode data and basic image info."""
    info = {"file": path, "size_kb": round(os.path.getsize(path) / 1024, 1)}

    if HAS_PYZBAR:
        img = Image.open(path)
        info["dimensions"] = f"{img.width}x{img.height}"
        codes = pyzbar.decode(img)
        info["qr_codes"] = [
            {"type": obj.type, "data": obj.data.decode("utf-8", errors="replace")}
            for obj in codes
        ]
    else:
        info["qr_codes"] = []
        info["note"] = "pyzbar not installed -- QR scanning skipped"

    return info


def run_ollama(prompt, timeout, image_path=None):
    """Send prompt (and optionally an image) to Ollama moondream and stream the response."""

    # Warmup / pre-load
    print(f"  Loading model {MODEL}...")
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "options": {"num_predict": 1},
                "keep_alive": "60m",
            },
            timeout=(10, 300),
        )
        r.raise_for_status()
        print("  Model ready.")
    except Exception as e:
        print(f"  WARNING: warmup failed ({e}), trying inference anyway...")

    # Base64-encode image for moondream vision
    user_message = {"role": "user", "content": prompt}
    if image_path and os.path.isfile(image_path):
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
        user_message["images"] = [img_b64]
        print(f"  Image attached: {image_path} (base64, {len(img_b64) // 1024} KB)")

    # Streaming inference
    print("  Running inference (streaming)...")
    start = time.time()
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [user_message],
            "stream": True,
            "options": {"num_predict": 512},
            "keep_alive": "60m",
        },
        timeout=(30, timeout),
        stream=True,
    )
    resp.raise_for_status()

    response_text = ""
    eval_count = 0
    buf = b""
    for data in resp.iter_content(chunk_size=4096):
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                chunk = json.loads(line)
                msg = chunk.get("message", {})
                token = msg.get("content", "")
                response_text += token
                if token:
                    print(token, end="", flush=True)
                if chunk.get("done"):
                    eval_count = chunk.get("eval_count", 0)

    elapsed = time.time() - start
    print()  # newline after streaming tokens

    return {
        "response": response_text.strip(),
        "model": MODEL,
        "tokens": eval_count,
        "duration_s": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Test Ollama LLM on an image — extract text, summarise, save JSON."
    )
    parser.add_argument("image", help="Path to the image file")
    parser.add_argument("-o", "--output", help="Output JSON path (default: <image>.ollama.json)")
    parser.add_argument("--timeout", type=int, default=120, help="Inference timeout in seconds (default 120)")
    args = parser.parse_args()

    if not os.path.isfile(args.image):
        sys.exit(f"Error: file not found: {args.image}")

    output_path = args.output or f"{args.image}.ollama.json"

    print(f"\n{'=' * 50}")
    print(f"  Ollama Test — {MODEL}")
    print(f"{'=' * 50}\n")

    # 1. Scan image for QR codes / metadata
    print("[1] Scanning image...")
    image_info = scan_image(args.image)
    print(f"    File: {image_info['file']} ({image_info['size_kb']} KB)")
    if image_info.get("dimensions"):
        print(f"    Dimensions: {image_info['dimensions']}")
    qr = image_info.get("qr_codes", [])
    if qr:
        print(f"    QR/Barcodes: {len(qr)}")
        for c in qr:
            print(f"      [{c['type']}] {c['data']}")
    else:
        print("    QR/Barcodes: none detected")

    # 2. Build prompt from extracted data
    parts = [f"Image file: {os.path.basename(args.image)} ({image_info['size_kb']} KB)"]
    if image_info.get("dimensions"):
        parts.append(f"Dimensions: {image_info['dimensions']}")
    if qr:
        parts.append("QR/Barcode data found:")
        for c in qr:
            parts.append(f"  [{c['type']}] {c['data']}")

    context = "\n".join(parts)
    prompt = (
        f"You are analysing a scanned conference badge image. "
        f"Describe what you see in the image. "
        f"Additional extracted data:\n{context}\n\n"
        f"Provide a brief summary of what this badge contains. "
        f"If QR code data is present, describe what it likely links to."
    )

    # 3. Run Ollama (with image for moondream vision)
    print(f"\n[2] Sending to Ollama ({MODEL}, timeout {args.timeout}s)...\n")
    try:
        llm_result = run_ollama(prompt, args.timeout, image_path=args.image)
    except requests.exceptions.ConnectionError:
        sys.exit("\nError: cannot connect to Ollama. Is it running?")
    except requests.exceptions.Timeout:
        sys.exit(f"\nError: Ollama timed out after {args.timeout}s.")

    # 4. Assemble and save JSON
    result = {
        "image": image_info,
        "prompt": prompt,
        "llm": llm_result,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n[3] Saved to {output_path}")
    print(f"    Tokens: {llm_result['tokens']}, Duration: {llm_result['duration_s']}s")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
