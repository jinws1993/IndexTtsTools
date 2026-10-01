# -*- coding: utf-8 -*-
"""把 .bat 文件统一转成 CRLF 换行 + 纯 ASCII。

cmd.exe 对 LF-only 的批处理文件解析不可靠，混合中文字节时会把
多字节字符截断成非法命令，导致"双击闪退"。此脚本做归一化。
"""
import io
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent

changed = []
for bat in sorted(DIR.glob("*.bat")):
    raw = bat.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    # 1) 去掉可能存在的 BOM（cmd 会把 BOM 当命令）
    if text.startswith("\ufeff"):
        text = text[1:]

    # 2) 非 ASCII 字符替换为安全占位（中文提示改由 Python 输出）
    bad = sorted({ch for ch in text if ord(ch) > 127})
    for ch in bad:
        text = text.replace(ch, "?")

    # 3) 统一 CRLF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\r\n")

    out = text.encode("ascii", errors="replace")
    if out != raw:
        bat.write_bytes(out)
        changed.append((bat.name, bad))

for name, bad in changed:
    note = f" (替换非ASCII字符: {''.join(bad)})" if bad else ""
    print(f"  已修正 {name}{note}")
if not changed:
    print("  所有 .bat 已是 CRLF + 纯 ASCII，无需修改")
