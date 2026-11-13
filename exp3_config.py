import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
TOP = "student-host:host"


def read_json(filename):
    # utf-8-sig also accepts files saved with a UTF-8 byte-order mark.
    return json.loads((HERE / filename).read_text(encoding="utf-8-sig"))


def collect():
    # Read configuration only; none of these commands changes settings.
    command = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$data = @{
    hostname = $env:COMPUTERNAME
    adapters = @(Get-NetAdapter -IncludeHidden |
        Select-Object Name, InterfaceIndex, InterfaceDescription,
                      MacAddress, InterfaceType, HardwareInterface)
    interfaces = @(Get-NetIPInterface |
        Select-Object InterfaceAlias, InterfaceIndex, AddressFamily,
                      NlMtu, ConnectionState, InterfaceMetric)
    addresses = @(Get-NetIPAddress |
        Select-Object InterfaceIndex, IPAddress, PrefixLength,
                      AddressFamily)
    routes = @(Get-NetRoute -AddressFamily IPv4 |
        Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } |
        Select-Object InterfaceIndex, NextHop, RouteMetric)
    dns = @(Get-DnsClientServerAddress |
        Select-Object InterfaceIndex, ServerAddresses)
}
$data | ConvertTo-Json -Depth 6
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout.lstrip("\ufeff"))


def build_observed(raw):
    adapters = {
        a["InterfaceIndex"]: a for a in raw["adapters"]
    }
    grouped = {}
    for row in raw["interfaces"]:
        grouped.setdefault(row["InterfaceIndex"], []).append(row)

    interfaces = []
    ipv4_rows = {}

    for index, rows in sorted(grouped.items()):
        # Prefer IPv4 MTU because this model has one MTU per interface.
        ipv4 = next(
            (r for r in rows if r["AddressFamily"] == 2), None
        )
        row = ipv4 if ipv4 is not None else rows[0]
        if ipv4 is not None:
            ipv4_rows[index] = ipv4

        adapter = adapters.get(index, {})
        name = row["InterfaceAlias"]
        description = adapter.get("InterfaceDescription", "").lower()
        interface_type = adapter.get("InterfaceType")

        if interface_type == 24 or name.startswith("Loopback"):
            kind = "loopback"
        elif "bluetooth" in description:
            kind = "other"
        elif adapter and adapter.get("HardwareInterface") is False:
            kind = "virtual"
        elif interface_type == 71:
            kind = "wifi"
        elif interface_type == 6:
            kind = "ethernet"
        else:
            kind = "other"

        item = {
            "name": name,
            "type": kind,
            "oper-status": (
                "up" if any(r["ConnectionState"] == 1 for r in rows)
                else "down"
            ),
            "mtu": int(row["NlMtu"]),
            "ipv4": [],
            "ipv6": [],
        }
	
	        # Preserve the original measurement in raw_windows.json.
        # The optional loopback MTU is omitted if the model cannot represent it.
        if kind == "loopback" and not 68 <= item["mtu"] <= 65536:
            reported_mtu = item.pop("mtu")
            print(
                f"MODEL NOTE: {name}: reported MTU {reported_mtu}; "
                "omitted optional mtu from observed.json. "
                "Original value retained in raw_windows.json."
            )
        mac = adapter.get("MacAddress")
        if mac:
            item["mac"] = mac.lower().replace("-", ":")

        for address in raw["addresses"]:
            if address["InterfaceIndex"] != index:
                continue
            family = address["AddressFamily"]
            if family in (2, 23):
                key = "ipv4" if family == 2 else "ipv6"
                value = (
                    f'{address["IPAddress"]}/{address["PrefixLength"]}'
                )
                item[key].append(value)

        item["ipv4"] = sorted(set(item["ipv4"]))
        item["ipv6"] = sorted(set(item["ipv6"]))
        interfaces.append(item)

    host = {
        "hostname": raw["hostname"],
        "os": "windows",
        "collected-at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "interface": interfaces,
        "dns-server": [],
    }

    # Choose a connected IPv4 default route by combined route/interface metric.
    routes = [
        r for r in raw["routes"]
        if r["InterfaceIndex"] in ipv4_rows
        and ipv4_rows[r["InterfaceIndex"]]["ConnectionState"] == 1
    ]
    if routes:
        route = min(
            routes,
            key=lambda r: (
                r["RouteMetric"]
                + ipv4_rows[r["InterfaceIndex"]]["InterfaceMetric"],
                r["InterfaceIndex"],
                r["NextHop"],
            ),
        )
        host["default-gateway"] = route["NextHop"]

        # This host-level DNS list represents the selected default interface.
        for entry in raw["dns"]:
            if entry["InterfaceIndex"] == route["InterfaceIndex"]:
                for server in entry.get("ServerAddresses") or []:
                    if server not in host["dns-server"]:
                        host["dns-server"].append(server)

    return {TOP: host}


def report_drift(intended, observed):
    differences = []

    def compare(path, wanted, actual):
        # Address and DNS lists are compared without depending on order.
        if isinstance(wanted, list) and isinstance(actual, list):
            equal = sorted(wanted) == sorted(actual)
        else:
            equal = wanted == actual
        if not equal:
            differences.append(
                f"{path}: intended {json.dumps(wanted)}, "
                f"observed {json.dumps(actual)}"
            )

    expected = intended[TOP]
    actual = observed[TOP]

    for field, value in expected.items():
        if field != "interface":
            compare(field, value, actual.get(field))

    by_name = {i["name"]: i for i in actual["interface"]}
    for interface in expected.get("interface", []):
        name = interface["name"]
        if name not in by_name:
            differences.append(
                f"interface {name}: intended present, observed missing"
            )
            continue
        for field, value in interface.items():
            if field != "name":
                compare(
                    f"interface {name} / {field}",
                    value,
                    by_name[name].get(field),
                )

    print("\nDRIFT REPORT")
    print("\n".join(differences) if differences else "No drift detected.")
    print("Only fields listed in intended.json were compared.")


def main():
    started = time.perf_counter()
    raw = collect()
    observed = build_observed(raw)

    # Keep original command data so conversions can be checked later.
    for filename, data in (
        ("raw_windows.json", raw),
        ("observed.json", observed),
    ):
        (HERE / filename).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    collection_seconds = time.perf_counter() - started
    print("Collected at:", observed[TOP]["collected-at"])
    print("Interfaces collected:", len(observed[TOP]["interface"]))
    print(f"Collection and JSON saving: {collection_seconds:.3f} seconds")
    print("Saved raw_windows.json and observed.json")

    schema = read_json("host_schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(observed),
        key=lambda e: "/".join(map(str, e.absolute_path)),
    )

    print("\nSCHEMA VALIDATION")
    if errors:
        print(f"INVALID: {len(errors)} error(s)")
        for error in errors:
            path = "/" + "/".join(map(str, error.absolute_path))
            print(f"{path}: {error.message}")
    else:
        print("VALID")

    # Drift is still useful for diagnosis when schema validation fails.
    report_drift(read_json("intended.json"), observed)
    print(f"\nTotal script time: {time.perf_counter() - started:.3f} seconds")


if __name__ == "__main__":
    main()