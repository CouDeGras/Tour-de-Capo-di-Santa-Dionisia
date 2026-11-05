"""Create a ten-year installation-local solar kernel using jplephem excerpt."""
import argparse
from datetime import date, timedelta
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from jplephem.calendar import compute_julian_date
from jplephem.daf import DAF
from jplephem.spk import SPK
from jplephem.commandline import RemoteFile

SOURCE = "https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp"
TARGETS = {3, 5, 6, 10, 399}
FALLBACK = Path(__file__).resolve().parent / "ephemeris" / "sun-earth-fallback.bsp"


def decade_window(today):
    try:
        anniversary = today.replace(year=today.year + 10)
    except ValueError:
        anniversary = today.replace(year=today.year + 10, day=28)
    # Local midnight can be on the previous UTC date; Sun observations
    # also need ephemeris samples from before the requested instant.
    return today - timedelta(days=2), anniversary + timedelta(days=2)


def validate_source(source, start, end):
    stream = RemoteFile(source) if source.startswith(("https://", "http://")) else open(source, "rb")
    with stream:
        kernel = SPK(DAF(stream))
        start_jd = compute_julian_date(start.year, start.month, start.day)
        end_jd = compute_julian_date(end.year, end.month, end.day)
        for target in TARGETS:
            matching = [s for s in kernel.segments if s.target == target]
            if not any(s.start_jd <= start_jd and s.end_jd >= end_jd for s in matching):
                raise ValueError("DE421 does not cover the full requested decade for target " + str(target))


def validate_excerpt(path):
    with SPK.open(str(path)) as kernel:
        if {s.target for s in kernel.segments} != TARGETS:
            raise ValueError("Solar excerpt is missing required targets")


def _prepare_fresh(output, source=SOURCE, today=None, ensure=False):
    output = Path(output)
    if ensure and output.is_file():
        validate_excerpt(output)
        return output
    start, end = decade_window(today or date.today())
    socket.setdefaulttimeout(60)
    validate_source(source, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="solar-excerpt-", dir=output.parent) as temporary:
        pending = Path(temporary) / "sun-earth.bsp"
        command = [sys.executable, "-m", "jplephem", "excerpt", "--targets", "3,5,6,10,399",
                   start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d"), source, str(pending)]
        print("Preparing solar data: " + start.isoformat() + " through " + end.isoformat(), flush=True)
        result = subprocess.run(command, capture_output=True, text=True, timeout=300,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "jplephem excerpt failed")
        validate_excerpt(pending)
        pending.replace(output)
    print("Solar data ready: " + str(output), flush=True)
    return output


def prepare(output, source=SOURCE, today=None, ensure=False, fallback=FALLBACK):
    today = today or date.today()
    try:
        return _prepare_fresh(output, source, today, ensure)
    except Exception as error:
        # A bundled excerpt can provide offline startup, but its fixed date
        # range must contain the current day and its timezone/light-time margin.
        validate_excerpt(fallback)
        validate_source(str(fallback), today - timedelta(days=2), today + timedelta(days=2))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="solar-fallback-", dir=output.parent) as temporary:
            pending = Path(temporary) / "sun-earth.bsp"
            shutil.copyfile(fallback, pending)
            validate_excerpt(pending)
            pending.replace(output)
        print("Fresh solar data unavailable: " + str(error), flush=True)
        print("Using bundled solar excerpt with fixed coverage; no new decade was generated.", flush=True)
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    data_root = Path(os.environ.get("CAPO_DI_SANTA_DIONISIA_DATA_DIR") or Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=data_root / "data" / "solar" / "sun-earth.bsp")
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--ensure", action="store_true")
    args = parser.parse_args()
    try:
        prepare(args.output, args.source, ensure=args.ensure)
    except Exception as error:
        print("Solar data preparation failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
