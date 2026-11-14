#!/bin/bash
# Downloads the DeepSeek pack into the volume from storage.sh. Run it in the background; progress goes to the log.
set -euo pipefail
PACK=${PACK:-DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
MNT=${MNT:-/mnt/models}
mountpoint -q "$MNT" || { echo "$MNT is not mounted: run storage.sh first" >&2; exit 1; }
pip install -q -U "huggingface_hub[hf_xet]"
# no chunk cache on the small VM disk, and the download's temp file stays on the same volume
export HF_XET_CHUNK_CACHE_SIZE_BYTES=0
export HF_HOME=$MNT/.hf
date
hf download jesdga/DeepSeek-V4.1-Flash-1017-Basalt "$PACK" --local-dir "$MNT"
ls -l "$MNT/$PACK"
date
echo DOWNLOAD_DONE
