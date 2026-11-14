#!/bin/bash
# Downloads a basalt pack (DeepSeek by default) into the volume from storage.sh. Run it in the background; progress goes to the log.
set -euo pipefail
REPO=${REPO:-jesdga/DeepSeek-V4.1-Flash-1017-Basalt}   # Qwen: jesdga/Qwen3.8-Flash-Next-Basalt
PACK=${PACK:-DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
MNT=${MNT:-/mnt/models}
mountpoint -q "$MNT" || { echo "$MNT is not mounted: run storage.sh first" >&2; exit 1; }
pip install -q -U "huggingface_hub[hf_xet]"
# no chunk cache on the small VM disk, and the download's temp file stays on the same volume
export HF_XET_CHUNK_CACHE_SIZE_BYTES=0
export HF_HOME=$MNT/.hf
date
hf download "$REPO" "$PACK" --local-dir "$MNT"
ls -l "$MNT/$PACK"
date
echo DOWNLOAD_DONE
