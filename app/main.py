#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import json
import inspect
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, is_dataclass, dataclass
from BookSearchV2.app.db.tools import iter_qc_issues_from_raw_json
from app.cli.parser import build_parser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import importlib
import pkgutil

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))

if __name__ == "__main__":
    raise SystemExit(main())