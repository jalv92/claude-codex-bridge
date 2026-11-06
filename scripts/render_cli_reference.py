#!/usr/bin/env python3
"""Render the actual command help as an SVG; no model or image-editing dependency."""
from html import escape
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
help_text = subprocess.check_output([sys.executable,
    str(ROOT / "tools/claude-codex-bridge/bridge.py"), "loop-begin", "--help"], text=True)
lines = ["$ python3 tools/claude-codex-bridge/bridge.py loop-begin --help", "", *help_text.rstrip().splitlines()]
height = 100 + len(lines) * 26
elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}" viewBox="0 0 1400 {height}" role="img" aria-labelledby="title">',
    '<title id="title">Actual loop-begin CLI help output</title>',
    f'<rect width="1400" height="{height}" rx="16" fill="#0d1117"/>',
    '<text x="32" y="38" font-family="monospace" font-size="18" fill="#8b949e">Claude–Codex Bridge · CLI reference</text>']
for i, line in enumerate(lines):
    color = "#79c0ff" if i == 0 else "#e6edf3"
    elements.append(f'<text x="32" y="{82+i*26}" xml:space="preserve" font-family="monospace" font-size="20" fill="{color}">{escape(line)}</text>')
elements.append('</svg>')
target = ROOT / "docs/assets/cli-reference.svg"
target.write_text("\n".join(elements) + "\n")
print(target.relative_to(ROOT))
