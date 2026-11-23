#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测入口：等价于 python cli.py eval，可独立运行。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["eval"] + sys.argv[1:]))
