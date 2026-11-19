# CSC6410 Experiment 4 - Run the remaining eleven default-selection trials.
# Purpose: keep counter collection and traffic execution in a fixed order.
# Usage at the Mininet prompt, from the experiment folder:
#   py __import__('run_ecmp_batch').run(h000)
# This module is called with a Mininet host; running it directly does not
# start the batch. Keep measure_ecmp.py in the same working folder.
#
# Assumptions: the default topology is running, h000 can reach 10.3.0.2,
# and an iperf3 server is already listening on h300 at port 5201.
# /usr/bin/iperf3 and python3 must be available. The first verified 16-flow
# trial was collected manually, so the plan deliberately omits (16, 1).
# Do not use this default-labelled batch to record a different configuration.
#
# Logic: save before counters, launch UDP traffic in h000, wait for it to
# finish, then save after counters and retain the complete traffic output.
# Each flow targets 1 Mbit/s for 20 seconds. Existing labels stop the batch.
# Testing: verify eleven result files and completed sender/receiver summaries,
# then combine them with the manual trial to obtain three runs per flow count.
# Check byte differences against the logs and inspect receiver loss directly.
#
# Limits: a zero return code and 'iperf Done.' check completion, not zero loss
# or an even path distribution. This code does not automatically validate
# every stream's rate, packet loss or the active switch hash configuration.
# A failed test may already have a counter result saved; exclude it from
# valid-run averages. A timeout can leave only partial logs and a baseline.
# Fallback: preserve partial files, check the server and connectivity, then
# repeat only the affected tests manually with fresh labels. The batch has
# no automatic resume function and stops if any planned label already exists.

import subprocess
from pathlib import Path


def run(host):
    folder = Path.cwd()
    measurement = folder / "measure_ecmp.py"

    # The first verified 16-flow run was already collected manually.
    plan = [
        (4, 1), (4, 2), (4, 3),
        (8, 1), (8, 2), (8, 3),
        (16, 2), (16, 3),
        (32, 1), (32, 2), (32, 3),
    ]

    def measure(action, label, log):
        command = [
            "python3", str(measurement), action, label
        ]
        # Read switch-port counters from the parent namespace, outside h000.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout + result.stderr
        print(output, end="", flush=True)
        log.write(output)
        log.flush()
        if result.returncode != 0:
            raise RuntimeError("Counter measurement failed.")

    for flows, repetition in plan:
        label = f"default_N{flows}_r{repetition}"
        log_path = folder / f"{label}_console.txt"
        traffic_path = folder / f"{label}_iperf.txt"

        # Preserve earlier evidence if a label has already been used.
        paths = [
            log_path,
            traffic_path,
            folder / f"{label}_before.json",
            folder / f"{label}_result.json",
        ]
        if any(path.exists() for path in paths):
            raise RuntimeError(
                f"{label} already has files. Stop and review them."
            )

        with log_path.open("x") as log:
            print(f"\nStarting {label}", flush=True)
            log.write(f"Run: {label}\n")
            measure("before", label, log)

            command = [
                "/usr/bin/iperf3",
                "-c", "10.3.0.2",
                "-p", "5201",
                "-u",
                "-b", "1M",
                "-P", str(flows),
                "-t", "20",
                "-i", "0",
            ]
            command_text = " ".join(command)
            print(command_text, flush=True)
            log.write(command_text + "\n")
            log.flush()

            # Start traffic inside h000 and wait for completion.
            with traffic_path.open("x") as traffic:
                process = host.popen(
                    command,
                    stdout=traffic,
                    stderr=subprocess.STDOUT,
                )
                try:
                    status = process.wait(timeout=60)
                except BaseException:
                    # Stop this client after a timeout or interruption.
                    process.kill()
                    process.wait()
                    raise

            # Preserve counter evidence even if the later completion check fails.
            measure("after", label, log)
            output = traffic_path.read_text()

            # Print summaries and preserve the complete traffic output.
            for line in output.splitlines():
                if "[SUM]" in line or "error" in line.lower():
                    print(line, flush=True)
            log.write(output)

            if status != 0 or "iperf Done." not in output:
                raise RuntimeError(
                    f"{label}: traffic failed. Review its iperf file."
                )

    print("\nAll 11 remaining normal runs completed.", flush=True)
