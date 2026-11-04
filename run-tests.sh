#!/bin/bash
# 交付前自检：逻辑测试 + 接口全链路
set -e
cd "$(dirname "$0")"
echo "== 逻辑测试 =="
if command -v swiftc >/dev/null 2>&1 && [ -f ios/Tests/MarkLogic.swift ]; then
  mkdir -p build
  swiftc -O -o build/rc_tests ios/Tests/MarkLogic.swift ios/Tests/TestMain.swift
  ./build/rc_tests
else
  echo "（跳过 Swift 逻辑测试：没有 swiftc 或 ios/Tests 不存在）"
fi
echo
echo "== 接口测试 =="
python3 tests-api.py
