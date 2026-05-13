from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLI_OUTPUT = ROOT / "cli_output.txt"
PIPELINE = (
    "build_student_profile.py",
    "select_institutes.py",
    "find_supervisors.py",
    "enrich_candidates.py",
    "draft_emails.py",
    "collect_feedback.py",
)


class _Tee:
    def __init__(self, stream, log_file) -> None:
        self._stream = stream
        self._log = log_file

    def write(self, data: str) -> int:
        self._stream.write(data)
        self._log.write(data)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        self._log.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()


def _run_script(script: Path) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


def main() -> None:
    with open(CLI_OUTPUT, "w", encoding="utf-8") as log:
        sys.stdout = _Tee(sys.__stdout__, log)
        sys.stderr = _Tee(sys.__stderr__, log)
        for script in PIPELINE:
            path = ROOT / script
            print(f"\n=== {script} ===\n", flush=True)
            if _run_script(path) != 0:
                raise SystemExit(1)
        print("\nPipeline finished.", flush=True)
        print(f"Log written to {CLI_OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
