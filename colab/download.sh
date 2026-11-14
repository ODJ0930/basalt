#!/bin/bash
# Downloads a basalt pack (DeepSeek by default) into the volume from storage.sh. Run it in the background; progress goes to the log.
# Anonymous downloads hit Hugging Face's rate limit (HTTP 429) after a few hundred GB: set HF_TOKEN (or save one to
# /content/hf_token) for a higher limit. A failed attempt is retried and resumes where it stopped.
set -uo pipefail
REPO=${REPO:-jesdga/DeepSeek-V4.1-Flash-1017-Basalt}   # Qwen: jesdga/Qwen3.8-Flash-Next-Basalt
PACK=${PACK:-DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
MNT=${MNT:-/mnt/models}
TRIES=${TRIES:-30}
mountpoint -q "$MNT" || { echo "$MNT is not mounted: run storage.sh first" >&2; exit 1; }
pip install -q -U "huggingface_hub[hf_xet]"
# no chunk cache on the small VM disk, and the download's temp file stays on the same volume
export HF_XET_CHUNK_CACHE_SIZE_BYTES=0
export HF_HOME=$MNT/.hf
if [ -z "${HF_TOKEN:-}" ] && [ -s /content/hf_token ]; then HF_TOKEN=$(cat /content/hf_token); export HF_TOKEN; fi
echo "token: $([ -n "${HF_TOKEN:-}" ] && echo yes || echo no (anonymous))"
for i in $(seq "$TRIES"); do
  date
  if hf download "$REPO" "$PACK" --local-dir "$MNT"; then
    ls -l "$MNT/$PACK"
    date
    echo DOWNLOAD_DONE
    exit 0
  fi
  echo "attempt $i failed; retrying in $((i < 5 ? 60 : 300)) s"
  sleep $((i < 5 ? 60 : 300))
done
echo DOWNLOAD_FAILED
exit 1
