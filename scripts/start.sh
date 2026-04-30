#!/bin/bash
# Crypto Quant 启动脚本
# 用法: bash scripts/start.sh
cd "$(dirname "$0")/.."
export PYTHONPATH=.
export PYTHONUNBUFFERED=1
exec .venv/bin/python3 -u main.py >> /tmp/crypto-quant.log 2>&1
