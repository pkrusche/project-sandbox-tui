"""Run a host agent in an external terminal and persist its lifetime.

Invoked by absolute filename so the target workspace need not be this package's
checkout. Uses only the standard library, including when run from a source tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run(summary: Path, argv: list[str]) -> int:
    def save(code: int | None = None) -> None:
        data = {"pid": os.getpid()}
        if code is not None:
            data["exit_code"] = code
        tmp = summary.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(summary)

    save()  # fail before starting an agent we cannot track
    try:
        proc = subprocess.Popen(argv)
    except OSError as exc:
        print(f"Could not start host agent: {exc}", file=sys.stderr)
        code = 127
    else:
        while True:
            try:
                code = proc.wait()
                break
            except KeyboardInterrupt:
                continue  # the foreground child received SIGINT too
    save(code)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]), sys.argv[2:]))
