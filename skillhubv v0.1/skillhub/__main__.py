#!/usr/bin/env python3
"""skillhub 命令行入口。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skillhub.cli import main

if __name__ == "__main__":
    sys.exit(main())
