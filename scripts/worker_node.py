#!/usr/bin/env python3
"""Entry point for a persistent local or cloud worker node."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.worker_client import main
if __name__ == '__main__':
    main()
