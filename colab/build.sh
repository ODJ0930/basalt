#!/bin/bash
# Builds basalt ($MODELS, DeepSeek only by default) for the Blackwell card with the CUDA 13 toolkit Colab ships, and installs the server's packages.
set -euo pipefail
SRC=${SRC:-/content/basalt}
MODELS=${MODELS:-deepseek41}   # qwen4exp, deepseek41, or "qwen4exp;deepseek41"
cd "$SRC"
pip install -q -r requirements.txt "cmake>=3.24" ninja
cmake --version | head -1
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release "-DBASALT_MODELS=$MODELS" \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc
cmake --build build -j "$(nproc)"
ls -l bin/
bin/basalt --version || true
echo BUILD_DONE
