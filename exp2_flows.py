#!/usr/bin/env python3

import os
import subprocess
from datetime import datetime

from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.log import setLogLevel


def run_command(*args):
    """Print each command and stop if it fails."""
    print("\n$ " + " ".join(args), flush=True)
    subprocess.run(args, check=True)


def main():
    # Root permissions are needed to create the virtual network.
    if os.geteuid() != 0:
        raise SystemExit("Run this script with sudo python3 exp2_flows.py")

    print("Time:", datetime.now().astimezone().isoformat(), flush=True)
    print("Environment: Ubuntu on WSL 2 | Mininet", flush=True)

    # This uses the kernel datapath that worked in my manual tests.
    # It assumes Open vSwitch is installed and its service is running.
    net = Mininet(controller=None, switch=OVSSwitch, build=False)

    try:
        switch = net.addSwitch(
            "s1", failMode="secure", datapath="kernel"
        )

        # Explicit port numbers keep the forwarding rules predictable.
        for number in range(1, 4):
            host = net.addHost(
                f"h{number}",
                ip=f"10.0.0.{number}/24",
                mac=f"00:00:00:00:00:{number:02x}",
            )
            net.addLink(host, switch, port2=number)

        net.build()
        net.start()

        run_command("ovs-vsctl", "get-fail-mode", "s1")
        run_command("ovs-ofctl", "show", "s1")
        run_command("ovs-ofctl", "del-flows", "s1")

        # ARP resolves neighbours before the hosts exchange IP packets.
        run_command(
            "ovs-ofctl", "add-flow", "s1",
            "priority=100,arp,actions=FLOOD",
        )

        # Each IP rule sends packets to the destination host's port.
        for number in range(1, 4):
            run_command(
                "ovs-ofctl", "add-flow", "s1",
                f"priority=100,ip,nw_dst=10.0.0.{number},"
                f"actions=output:{number}",
            )

        print("\n=== FLOW TABLE BEFORE PING ===", flush=True)
        run_command("ovs-ofctl", "dump-flows", "s1")

        print("\n=== CONNECTIVITY TEST ===", flush=True)
        loss = net.pingAll(timeout="2")
        print(f"Measured packet loss: {loss:.1f}%", flush=True)

        print("\n=== FLOW TABLE AFTER PING ===", flush=True)
        run_command("ovs-ofctl", "dump-flows", "s1")

        # This checks basic connectivity, not performance or every policy.
        if loss != 0:
            raise RuntimeError(
                "Some pings failed. Check the printed rules and ports."
            )

    finally:
        # Clean up even if a command fails. Errors remain visible.
        # If cleanup is incomplete, run sudo mn -c before retrying.
        net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    main()
