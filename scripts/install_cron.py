"""Schedule nightly Memoria backup via cron.

Usage:
    python3 scripts/install_cron.py            # install default (03:15 daily)
    python3 scripts/install_cron.py --hour 4   # 04:15 daily
    python3 scripts/install_cron.py --uninstall

Adds a single crontab entry:
    15 3 * * *  cd /home/ubuntu/memoria && /usr/bin/python3 bin/memoria backup all >> /home/ubuntu/memoria/data/backup.log 2>&1
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
CRON_LINE = (
    "15 {hour} * * * "
    f"cd {ROOT} && {PYTHON} -m memoria.bin.memoria backup all "
    f">> {ROOT}/data/backup.log 2>&1"
)


def _read_cron() -> str:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
        return out.stdout if out.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _write_cron(content: str) -> None:
    p = subprocess.run(
        ["crontab", "-"], input=content, text=True, check=True,
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"crontab write failed: {p.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hour", type=int, default=3, help="hour to run (0-23)")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    current = _read_cron()
    lines = [l for l in current.splitlines() if "memoria" not in l]

    if args.uninstall:
        _write_cron("\n".join(lines) + "\n")
        print("Memoria cron entry removed (if it existed).")
        return 0

    new_line = CRON_LINE.format(hour=args.hour)
    lines.append(new_line)
    _write_cron("\n".join(lines) + "\n")
    print(f"Installed: {new_line}")
    print("Verify with: crontab -l | grep memoria")
    return 0


if __name__ == "__main__":
    sys.exit(main())