# Purpose: This script reads my saved traceroutes and maps public
# hop addresses to their announcing AS numbers and network names.

# Assumption: The input contains one English Windows IPv4 traceroute
# per file. This parser is not designed for IPv6 traces.

# Testing: I ran the script on both measurement rounds for five sites.
# I also compared the Microsoft destination mapping with the
# RIPEstat response obtained separately through PowerShell.

# Limits: Unanswered hops can hide networks from the observed path.
# An announcing AS does not prove which network forwarded the packet.
# Cached responses may not reflect later changes in announcements.

import argparse
import ipaddress
import json
import re
import time
from pathlib import Path

import requests


CACHE_FILE = Path("ripestat_cache.json")
BASE_URL = "https://stat.ripe.net/data"

# I skip private and shared addresses because they cannot be mapped
# to public BGP origins in the same way as public hop addresses.
SKIP_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
]

# Match hop rows without treating timestamps or headers as hops.
HOP_ROW = re.compile(r"^\s*(\d+)\s+(?:\*|<?\d+\s+ms)")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def read_text(path):
    # PowerShell may save files using UTF-16.
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def is_public(address):
    ip = ipaddress.ip_address(address)
    if any(ip in network for network in SKIP_NETWORKS):
        return False
    return ip.is_global


# I mask the first public hop in the printed output.
# The original input files and API cache still contain full addresses.

def display_ip(address, hidden_ip):
    # Redact printed output but retain the full IP for API queries.
    if address == hidden_ip:
        return ".".join(address.split(".")[:2]) + ".x.x"
    return address

# I reuse saved API answers to avoid requesting the same data again.
# New requests have a delay and a timeout to avoid excessive requests
# and prevent the script from waiting indefinitely.

def api(endpoint, resource, cache):
    key = f"{endpoint}:{resource}"

    if key in cache:
        return cache[key]

    # Make requests sequentially and pause between uncached requests.
    time.sleep(1)

    response = requests.get(
        f"{BASE_URL}/{endpoint}/data.json",
        params={"resource": resource},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("status") != "ok":
        raise ValueError("RIPEstat did not return an OK response.")

    data = payload["data"]
    cache[key] = data

    CACHE_FILE.write_text(
        json.dumps(cache, indent=2),
        encoding="utf-8",
    )

    return data


# I extract the destination and numbered hop rows from the saved text.
# A row with no responding IPv4 address remains an unknown hop.

def parse_trace(text):
    destination = None
    hops = []

    for line in text.splitlines():
        if "Tracing route to" in line:
            match = re.search(r"\[([\d.]+)\]", line)
            if match:
                destination = match.group(1)

        match = HOP_ROW.match(line)
        if not match:
            continue

        addresses = []

        for candidate in IPV4.findall(line):
            try:
                ipaddress.IPv4Address(candidate)
            except ValueError:
                continue

            if candidate not in addresses:
                addresses.append(candidate)

        hops.append((int(match.group(1)), addresses))

    return destination, hops


def map_trace(path, cache):
    destination, hops = parse_trace(read_text(path))

    if not hops:
        raise ValueError("No IPv4 traceroute hop rows found.")

    # Identify the first public hop for redaction.
    hidden_ip = next(
        (
            address
            for _, addresses in hops
            for address in addresses
            if is_public(address)
        ),
        None,
    )

    path_parts = []
    observed_asns = set()

    print(f"\nFILE: {path.name}")
    print("Lookup time:", time.strftime("%Y-%m-%d %H:%M:%S %z"))

    for number, addresses in hops:
        if not addresses:
            print(f"Hop {number}: no reply")
            label = "?"

        else:
            labels = []

            for address in addresses:
                shown = display_ip(address, hidden_ip)

                if not is_public(address):
                    print(
                        f"Hop {number}: {shown} | "
                        "private/shared/non-global; skipped"
                    )
                    labels.append("[skipped]")
                    continue

                try:
                    info = api("network-info", address, cache)

                    asns = sorted(
                        {
                            str(value)
                            for value in info.get("asns", [])
                        },
                        key=int,
                    )

                    prefix = info.get("prefix") or "unknown"

                    if not asns:
                        print(
                            f"Hop {number}: {shown} | "
                            "no origin ASN returned"
                        )
                        labels.append("?")
                        continue

                    names = []

                    for asn in asns:
                        observed_asns.add(asn)

                        try:
                            overview = api(
                                "as-overview",
                                f"AS{asn}",
                                cache,
                            )
                            holder = (
                                overview.get("holder")
                                or "name unavailable"
                            )

                        except (
                            requests.RequestException,
                            ValueError,
                            KeyError,
                        ):
                            holder = "name lookup failed"

                        names.append(f"AS{asn} - {holder}")

                    print(
                        f"Hop {number}: {shown} | {prefix} | "
                        + "; ".join(names)
                    )

                    labels.append(
                        "/".join(f"AS{asn}" for asn in asns)
                    )

                except (
                    requests.RequestException,
                    ValueError,
                    KeyError,
                ):
                    print(
                        f"Hop {number}: {shown} | API lookup failed"
                    )
                    labels.append("?")

            label = " | ".join(dict.fromkeys(labels))

        # Collapse consecutive duplicates without hiding unknown gaps.
        if not path_parts or label != path_parts[-1]:
            path_parts.append(label)

    reached = (
        destination is not None
        and destination in hops[-1][1]
    )

    print(
        "\nDestination reached:",
        "yes" if reached else "not confirmed",
    )

    if reached:
        print("IP hop count:", hops[-1][0])
    else:
        print("Highest probed hop:", hops[-1][0])

    print("Observed AS path:", " -> ".join(path_parts))
    print("Distinct mapped ASNs:", len(observed_asns))
    print("Note: ? marks missing information, not an AS.")
    print(
        "IP-to-AS mapping is an inference from announcing prefixes."
    )

    if destination and is_public(destination):
        try:
            info = api("network-info", destination, cache)

            print(
                "Destination IP:",
                display_ip(destination, hidden_ip),
            )
            print("Destination prefix:", info.get("prefix"))
            print(
                "Destination origin ASN(s):",
                info.get("asns"),
            )

        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ):
            print("Destination origin lookup failed.")


def main():
    parser = argparse.ArgumentParser(
        description="Map saved Windows IPv4 traceroutes using RIPEstat."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Saved IPv4 traceroute files",
    )
    args = parser.parse_args()

    if CACHE_FILE.exists():
        # Do not silently overwrite a damaged cache.
        cache = json.loads(
            CACHE_FILE.read_text(encoding="utf-8")
        )
    else:
        cache = {}

    for filename in args.files:
        try:
            map_trace(Path(filename), cache)

        except (OSError, UnicodeError, ValueError) as error:
            print(
                f"Could not process {Path(filename).name}: {error}"
            )


if __name__ == "__main__":
    main()