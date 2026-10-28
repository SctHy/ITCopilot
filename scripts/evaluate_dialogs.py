#!/usr/bin/env python3
"""Compatibility entry point for real HTTP/application evaluation.

Run with the same arguments as evaluate_application.py. To grade prose afterwards,
use judge_application_report.py; correctness remains a business-state assertion.
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.evaluate_application import main

if __name__ == "__main__":
    asyncio.run(main())
