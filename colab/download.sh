#!/bin/bash
# Downloads a basalt pack (DeepSeek by default) into the volume from storage.sh. Run it in the background; progress goes to the log.
# A single pack is fetched with aria2 (8 ranged connections, resumable): Hugging Face rate-limits hard (HTTP 429), and
# `hf download` drops a half-finished file on every 429, while aria2 keeps what it has and continues.
# INCLUDE (a glob such as "unsloth-UD-IQ4_XS/*") fetches a set of files with `hf download` instead, e.g. GGUF shards to convert.
# Set HF_TOKEN (or save one to /content/hf_token) for a higher rate limit.
set -uo pipefail
REPO=${REPO:-jesdga/DeepSeek-V4.1-Flash-1017-Basalt}   # Qwen: jesdga/Qwen3.8-Flash-Next-Basalt
PACK=${PACK:-DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
INCLUDE=${INCLUDE:-}
MNT=${MNT:-/mnt/models}
TRIES=${TRIES:-30}
if [ "${MNT#/mnt/}" != "$MNT" ]; then   # a volume made by storage.sh or a tmpfs: it must be mounted
  mountpoint -q "$MNT" || { echo "$MNT is not mounted: run storage.sh first" >&2; exit 1; }
fi
mkdir -p "$MNT"
if [ -z "${HF_TOKEN:-}" ] && [ -s /content/hf_token ]; then HF_TOKEN=$(cat /content/hf_token); export HF_TOKEN; fi
if [ -n "${HF_TOKEN:-}" ]; then echo "token: yes"; AUTH=(--header="Authorization: Bearer $HF_TOKEN"); else echo "token: no (anonymous)"; AUTH=(); fi

if [ -z "$INCLUDE" ]; then
  command -v aria2c > /dev/null || flock /tmp/basalt-apt.lock apt-get -qq install -y aria2 > /dev/null
  for i in $(seq "$TRIES"); do
    date
    if aria2c -c -x 8 -s 8 -k 64M --file-allocation=none --console-log-level=warn --summary-interval=60 \
         --max-tries=0 --retry-wait=20 "${AUTH[@]}" -d "$MNT" -o "$PACK" "https://huggingface.co/$REPO/resolve/main/$PACK"; then
      ls -l "$MNT/$PACK"; date
      echo DOWNLOAD_DONE
      exit 0
    fi
    echo "attempt $i stopped; resuming in 30 s"
    sleep 30
  done
  echo DOWNLOAD_FAILED
  exit 1
fi

# parallel downloads share one pip lock: two concurrent upgrades leave huggingface_hub half-installed
flock /tmp/basalt-pip.lock pip install -q -U "huggingface_hub[hf_xet]"
# no chunk cache on the small VM disk, and the download's temp file stays on the same volume
export HF_XET_CHUNK_CACHE_SIZE_BYTES=0
export HF_HOME=$MNT/.hf
for i in $(seq "$TRIES"); do
  date
  if hf download "$REPO" --include "$INCLUDE" --local-dir "$MNT"; then
    ls -l "$MNT/${INCLUDE%%/*}"; date
    echo DOWNLOAD_DONE
    exit 0
  fi
  echo "attempt $i failed; retrying in $((i < 5 ? 60 : 300)) s"
  sleep $((i < 5 ? 60 : 300))
done
echo DOWNLOAD_FAILED
exit 1
