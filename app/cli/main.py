from __future__ import annotations
from typing import Optional, Sequence

from app.cli.parser import build_parser

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))