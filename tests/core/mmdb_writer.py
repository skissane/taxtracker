"""A minimal MaxMind DB writer, enough for DB-IP Lite-shaped test databases
(for tests/core/test_client_ips.py and the screenshot tests): an IPv6 tree with
24-bit records, IPv4 under ::/96 as in DB-IP's files, and maps, lists, strings
and unsigned ints as data. No Django imports, so the screenshot tests can
import it before Django is set up.
"""

import ipaddress
import struct
from pathlib import Path

_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def _control(type_, size):
    extended = type_ > 7
    first = (0 if extended else type_) << 5
    if size < 29:
        out = bytes([first | size])
    else:
        out = bytes([first | 29]) + bytes([size - 29])
    if extended:
        out = out[:1] + bytes([type_ - 7]) + out[1:]
    return out


class _UInt16(int):
    pass


class _UInt64(int):
    pass


def _encode(value):
    if isinstance(value, dict):
        return _control(7, len(value)) + b"".join(_encode(k) + _encode(v) for k, v in value.items())
    if isinstance(value, list):
        return _control(11, len(value)) + b"".join(_encode(v) for v in value)
    if isinstance(value, str):
        data = value.encode()
        return _control(2, len(data)) + data
    if isinstance(value, int):
        # libmaxminddb (which maxminddb uses if it can) checks the metadata's
        # types, so some are uint16 or uint64 rather than uint32.
        type_ = 5 if isinstance(value, _UInt16) else 9 if isinstance(value, _UInt64) else 6
        data = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
        return _control(type_, len(data)) + data
    raise TypeError(value)


def write_mmdb(path, networks, database_type="DBIP-Test"):
    """Write ``networks`` ({"203.0.113.0/24": record, ...}) as a MaxMind DB."""
    nodes = [[None, None]]
    data = b""
    for network, record in networks.items():
        net = ipaddress.ip_network(network)
        if net.version == 4:
            bits = 96 + net.prefixlen
            value = int(net.network_address)
        else:
            bits = net.prefixlen
            value = int(net.network_address)
        offset = len(data)
        data += _encode(record)
        node = 0
        for i in range(bits):
            bit = (value >> (127 - i)) & 1
            if i == bits - 1:
                nodes[node][bit] = ("data", offset)
            else:
                if nodes[node][bit] is None:
                    nodes.append([None, None])
                    nodes[node][bit] = ("node", len(nodes) - 1)
                node = nodes[node][bit][1]
    count = len(nodes)

    def record_value(entry):
        if entry is None:
            return count
        kind, number = entry
        return number if kind == "node" else count + 16 + number

    tree = b"".join(
        struct.pack(">I", record_value(left))[1:] + struct.pack(">I", record_value(right))[1:] for left, right in nodes
    )
    metadata = {
        "node_count": count,
        "record_size": _UInt16(24),
        "ip_version": _UInt16(6),
        "database_type": database_type,
        "languages": ["en"],
        "binary_format_major_version": _UInt16(2),
        "binary_format_minor_version": _UInt16(0),
        "build_epoch": _UInt64(1_760_000_000),
        "description": {"en": "test"},
    }
    Path(path).write_bytes(tree + b"\0" * 16 + data + _METADATA_MARKER + _encode(metadata))


def write_geoip(directory, month="2026-10"):
    directory.mkdir(parents=True, exist_ok=True)
    write_mmdb(
        directory / f"dbip-country-lite-{month}.mmdb",
        {
            "203.0.113.0/24": {"country": {"iso_code": "AU", "names": {"en": "Australia"}}},
            "2001:db8::/32": {"country": {"iso_code": "NZ", "names": {"en": "NZ"}}},
        },
    )
    write_mmdb(
        directory / f"dbip-asn-lite-{month}.mmdb",
        {
            "203.0.113.0/24": {
                "autonomous_system_number": 64500,
                "autonomous_system_organization": "Example ISP",
            }
        },
    )
