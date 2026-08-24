#!/usr/bin/env python3
"""Install the source-controlled reflect-setup skill for Claude or Codex."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from runtime import install_result_dict, install_skill, resolve_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("HOME", ".")))
    parser.add_argument("--mode", choices=("symlink", "copy"), default="symlink")
    args = parser.parse_args()
    runtime_name = None if args.runtime == "auto" else args.runtime
    spec = resolve_runtime(runtime_name, home=args.home, env=os.environ)
    result = install_skill(spec, args.source, mode=args.mode)
    print(json.dumps(install_result_dict(result), sort_keys=True))


if __name__ == "__main__":
    main()
