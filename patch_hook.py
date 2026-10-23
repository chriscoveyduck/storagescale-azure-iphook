"""Apply the tested Azure CES hook changes to an entitled IBM original, offline."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import uuid


VENDOR_SHA256 = "03412ece207f3a0be65f95a8bf55a9959f9b208e2d77c0973930604b0c318e4c"
PATCH = Path(__file__).resolve().parent / "azure-ces.patch.in"


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    expected = {"subscription", "resource_group", "addresses", "ip_config_prefix", "nodes"}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError(f"Configuration must contain exactly: {', '.join(sorted(expected))}")
    subscription = config["subscription"]
    if not isinstance(subscription, str) or str(uuid.UUID(subscription)) != subscription:
        raise ValueError("subscription must be a canonical lowercase UUID")
    for key in ("resource_group", "ip_config_prefix"):
        validate_name(config[key], key)
    nodes = config["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 2:
        raise ValueError("Exactly two protocol nodes are supported")
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"vm", "nic", "ces_node"}:
            raise ValueError("Each node must contain vm, nic and ces_node")
        for key, value in node.items():
            validate_name(value, key)
    for key in ("vm", "nic", "ces_node"):
        if nodes[0][key].lower() == nodes[1][key].lower():
            raise ValueError(f"Node {key} values must be distinct")
    addresses = config["addresses"]
    if not isinstance(addresses, list) or not addresses:
        raise ValueError("addresses must be a nonempty list of CES service IPv4 addresses")
    for address in addresses:
        if not isinstance(address, str) or str(ipaddress.IPv4Address(address)) != address:
            raise ValueError("Only canonical IPv4 addresses are supported")
    if len(set(addresses)) != len(addresses):
        raise ValueError("Duplicate CES addresses")
    return config


def validate_name(value, label):
    # Values become shell case patterns and quoted literals, never shell code.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError(f"{label} must be 1-80 ASCII letters, digits, underscores, dots or hyphens")


def render_patch(config):
    values = {
        "SUBSCRIPTION": config["subscription"],
        "RESOURCE_GROUP": config["resource_group"].lower(),
        "ADDRESSES": "|".join(config["addresses"]),
        "IP_CONFIG_PREFIX": config["ip_config_prefix"],
    }
    for index, node in enumerate(config["nodes"], 1):
        values.update({f"NODE{index}_{key.upper()}": value for key, value in node.items()})
    patch = PATCH.read_text(encoding="utf-8")
    for key, value in values.items():
        patch = patch.replace(f"@{key}@", value)
    if re.search(r"@[A-Z0-9_]+@", patch):
        raise ValueError("Unresolved patch template token")
    return patch


def apply_unified_patch(original, patch):
    source = original.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        raise ValueError("Expected a single-file unified diff")
    output, cursor, index = [], 0, 2
    while index < len(lines):
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n", lines[index])
        if not header:
            raise ValueError("Invalid patch hunk header")
        old_start = int(header[1]) - 1
        old_count = int(header[2]) if header[2] is not None else 1
        new_count = int(header[4]) if header[4] is not None else 1
        if old_start < cursor or old_start > len(source):
            raise ValueError("Overlapping or out-of-range patch hunk")
        output.extend(source[cursor:old_start])
        if len(output) != int(header[3]) - 1:
            raise ValueError("Patch output offset mismatch")
        cursor = old_start
        removed = added = 0
        index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line[0] not in " +-":
                raise ValueError("Unsupported patch line")
            if line[0] in " -":
                if cursor >= len(source) or source[cursor] != line[1:]:
                    raise ValueError("Patch context does not match vendor original")
                cursor += 1
                removed += 1
            if line[0] in " +":
                output.append(line[1:])
                added += 1
            index += 1
        if (removed, added) != (old_count, new_count):
            raise ValueError("Patch hunk length mismatch")
    output.extend(source[cursor:])
    return "".join(output)


def patch_hook(original, config):
    if hashlib.sha256(original).hexdigest() != VENDOR_SHA256:
        raise ValueError("IBM original SHA256 mismatch; refusing another version or an already patched hook")
    source = original.decode("utf-8")
    candidate = apply_unified_patch(source, render_patch(config))
    prefix = source[:source.index("function cleanupIP {")]
    dispatcher = source[source.index('print -- "callout='):]
    if not candidate.startswith(prefix) or candidate[candidate.index('print -- "callout='):] != dispatcher:
        raise ValueError("Top-level declarations or dispatcher changed")
    return candidate.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="Unmodified IBM-supplied mmcesExtendedIpMgmt.azure")
    parser.add_argument("output", type=Path, help="New scratch file; must not exist")
    parser.add_argument("--config", type=Path, required=True, help="Explicit two-node environment JSON")
    args = parser.parse_args()
    try:
        candidate = patch_hook(args.original.read_bytes(), load_config(args.config))
        # Exclusive creation prevents overwriting either the original or an active hook.
        with args.output.open("xb") as handle:
            args.output.chmod(0o700)
            handle.write(candidate)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"SHA256 {hashlib.sha256(candidate).hexdigest()}  {args.output}")
    print("Created offline candidate only; no hook was installed or executed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
