"""Streams one chat request to the local basalt server and reports time to first token and generation speed.

    python3 colab/test.py ["your prompt"] [--url http://127.0.0.1:8090] [--max-tokens 400]
"""
import argparse
import json
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("prompt", nargs="?", default="用三句话介绍你自己，然后用 Python 写一个快速排序。")
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--max-tokens", type=int, default=400)
args = ap.parse_args()

key = open("/content/basalt_api_key.txt").read().strip()
auth = {"Authorization": f"Bearer {key}"}
with urllib.request.urlopen(urllib.request.Request(f"{args.url}/v1/models", headers=auth), timeout=30) as r:
    model = json.load(r)["data"][0]["id"]

body = {"model": model, "max_tokens": args.max_tokens, "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": args.prompt}]}
req = urllib.request.Request(f"{args.url}/v1/chat/completions", json.dumps(body).encode(),
                             {**auth, "Content-Type": "application/json"})
start = time.time()
first = None
pieces = 0
usage = None
with urllib.request.urlopen(req, timeout=900) as r:
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[5:])
        usage = chunk.get("usage") or usage
        for ch in chunk.get("choices", []):
            delta = ch.get("delta", {})
            text = delta.get("content") or delta.get("reasoning_content") or ""
            if text:
                if first is None:
                    first = time.time()
                pieces += 1
                print(text, end="", flush=True)
end = time.time()
tokens = (usage or {}).get("completion_tokens") or pieces
print(f"\n\n--- model: {model}")
print(f"--- first token after {((first or end) - start):.2f} s; {tokens} tokens in {end - start:.1f} s")
if first and end > first:
    print(f"--- generation speed: {tokens / (end - first):.1f} tok/s")
