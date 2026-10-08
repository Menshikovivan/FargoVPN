#!/usr/bin/env python3
"""Compatibility entry point; actual restore logic lives in restore_manager.py."""
from restore_manager import cli
if __name__ == "__main__": raise SystemExit(cli())
