# CSC6410 Experiment 4 - Measure the distribution of transmitted bytes.
# Purpose: compare traffic on the four core-facing links leaving pod 0.
# Usage at the Mininet prompt:
#   sh python3 measure_ecmp.py before RUN_LABEL
#   Run one traffic test and wait for it to finish.
#   sh python3 measure_ecmp.py after RUN_LABEL
# Use the same label for the paired readings and a new label for each trial.
#
# Logic: save cumulative counters, subtract the baseline, then calculate
# each link's byte share and the maximum-to-mean ratio across all four links.
# Assumptions: the supplied topology is running with these interface names,
# and both readings refer to the same interfaces without a topology rebuild.
# The counters must be readable from the namespace running this script.
# Equal-rate, equal-duration flows make byte share an approximate flow share.
#
# Testing: compare saved before/after values with the printed differences.
# Equal positive differences should give 25% per link and a ratio of 1.000.
# Check zero traffic, duplicate labels and negative differences separately.
# The verified first 16-flow lab trial gave about 50% on each of two links.
#
# Limits: counters include all transmitted traffic, not just the test flows.
# This script does not inspect iperf completion, loss, or individual paths.
# A small or failed workload can still produce a numerical share; check the
# traffic log before accepting a result. Negative differences detect some
# counter resets, but a reset followed by enough traffic may go undetected.
# Fallback: if ports are missing, check the topology and execution namespace.
# If counters reset or a trial fails, preserve its evidence and start a new
# before/traffic/after sequence with a fresh label. Do not reuse its baseline.

import argparse
import json
from datetime import datetime
from pathlib import Path

PORTS = ["a00-eth3", "a00-eth4", "a01-eth3", "a01-eth4"]
HERE = Path(__file__).resolve().parent


def read_counters():
    # Read cumulative transmitted bytes from the four core-facing ports.
    return {
        port: int(
            Path(f"/sys/class/net/{port}/statistics/tx_bytes").read_text()
        )
        for port in PORTS
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["before", "after"])
    parser.add_argument("label")
    args = parser.parse_args()

    if not all(c.isalnum() or c in "_-" for c in args.label):
        parser.error("Use only letters, numbers, underscores or hyphens.")

    before_file = HERE / f"{args.label}_before.json"
    result_file = HERE / f"{args.label}_result.json"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    current = read_counters()

    if args.action == "before":
        if before_file.exists():
            raise FileExistsError("Choose a new label to preserve earlier data.")
        before_file.write_text(json.dumps(
            {"time": now, "counters": current}, indent=2
        ))
        print("Before counters saved:", before_file.name)
        for port, value in current.items():
            print(port, value)
        return

    if result_file.exists():
        raise FileExistsError("A result already exists for this label.")

    # Subtract cumulative readings rather than treating totals as run bytes.
    before = json.loads(before_file.read_text())
    delta = {
        port: current[port] - before["counters"][port]
        for port in PORTS
    }
    if any(value < 0 for value in delta.values()):
        raise ValueError("Counters reset; repeat with a new label.")

    total = sum(delta.values())
    print(f"\nRun: {args.label}")
    print(f"{'Link':<12} {'Before':>12} {'After':>12} "
          f"{'Bytes':>12} {'Share':>9}")
    for port in PORTS:
        share = 100 * delta[port] / total if total else 0
        print(f"{port:<12} {before['counters'][port]:>12} "
              f"{current[port]:>12} {delta[port]:>12} {share:>8.2f}%")

    # Keep all four links in the mean, including links carrying zero bytes.
    # When total is zero, printed row shares are placeholders; metrics are undefined.
    busiest = 100 * max(delta.values()) / total if total else None
    ratio = 4 * max(delta.values()) / total if total else None

    if total:
        print(f"Busiest share: {busiest:.2f}%")
        print(f"Max/mean across four links: {ratio:.3f}")
    else:
        print("No transmitted bytes; shares and ratio are undefined.")

    result_file.write_text(json.dumps({
        "label": args.label,
        "before_time": before["time"],
        "after_time": now,
        "before": before["counters"],
        "after": current,
        "bytes": delta,
        "total_bytes": total,
        "busiest_share_percent": busiest,
        "max_mean_four_links": ratio
    }, indent=2))
    print("Saved:", result_file.name)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"Measurement failed: {error}")
