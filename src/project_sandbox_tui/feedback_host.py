"""Show host reporting instructions and exec the agent (stdlib only)."""

import os
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(sys.argv[1])
    print(f"Run feedback skill: {root / 'skill' / 'SKILL.md'}", flush=True)
    print(f"Reporting context: {root / 'context.json'}", flush=True)
    try:
        os.execvp(sys.argv[2], sys.argv[2:])
    except OSError as exc:
        print(f"Could not start host agent: {exc}", file=sys.stderr)
        sys.exit(127)
