#!/usr/bin/env python3
"""Wrapper preserving ptr.py entrypoint for trader-pelosi (NSE Bulk/Block Deals & Insider Tracker)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deals

if __name__ == "__main__":
    sys.exit(deals.main())
