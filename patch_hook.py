"""Generate a self-discovering Azure CES hook from an entitled IBM original."""

import argparse
import hashlib
import os
from pathlib import Path
import sys


VENDOR_SHA256 = "03412ece207f3a0be65f95a8bf55a9959f9b208e2d77c0973930604b0c318e4c"
FUNCTIONS = Path(__file__).resolve().parent / "azure_ces_functions.ksh"


def patch_hook(original):
    if hashlib.sha256(original).hexdigest() != VENDOR_SHA256:
        raise ValueError("IBM original SHA256 mismatch; refusing another version or an already patched hook")
    source = original.decode("utf-8")
    start = source.index("function cleanupIP {")
    end = source.index('print -- "callout=')
    functions = FUNCTIONS.read_text(encoding="utf-8")
    for name in ("cleanupIP", "enableIP", "deleteIP"):
        if functions.count(f"function {name}\n") != 1:
            raise ValueError(f"Expected exactly one {name} replacement")
    return (source[:start] + functions + "\n" + source[end:]).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="Unmodified IBM-supplied mmcesExtendedIpMgmt.azure")
    parser.add_argument("output", type=Path, help="New output file; must not exist")
    args = parser.parse_args()
    try:
        candidate = patch_hook(args.original.read_bytes())
        write_output(args.output, candidate)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"SHA256 {hashlib.sha256(candidate).hexdigest()}  {args.output}")
    print("Created patched copy only; no hook was installed or executed.")
    return 0


def write_output(path, candidate):
    handle = path.open("xb")
    identity = None
    try:
        with handle:
            identity = os.fstat(handle.fileno())
            path.chmod(0o700)
            handle.write(candidate)
    except BaseException:
        # Do not remove another file installed at the destination during a failure.
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if identity is not None and current is not None:
            if os.path.samestat(identity, current):
                path.unlink()
            else:
                print(f"warning: output was replaced; leaving {path} untouched", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
