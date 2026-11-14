#!/bin/bash
# Builds basalt (DeepSeek only) for the Blackwell card with the CUDA 13 toolkit Colab ships, and installs the server's packages.
set -euo pipefail
SRC=${SRC:-/content/basalt}
cd "$SRC"
pip install -q -r requirements.txt "cmake>=3.24" ninja
cmake --version | head -1
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBASALT_MODELS=deepseek41 \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc
cmake --build build -j "$(nproc)"
ls -l bin/
bin/basalt --version || true
echo BUILD_DONE
