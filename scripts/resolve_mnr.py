"""Resolve the ministry hostname over HTTPS when runner DNS fails.

Print only freshly resolved public IPv4 hosts-file entries to stdout. The
workflow applies them only to its disposable runner; TLS still verifies the
original ministry hostname. This is DNS recovery, not a content proxy.
"""

from __future__ import annotations

import ipaddress
import sys

import requests


HOST = "www.mnr.gov.cn"
RESOLVERS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")


def public_addresses(payload: dict) -> list[str]:
    if payload.get("Status") != 0:
        return []
    answers = payload.get("Answer", [])
    names = {HOST}
    # Accept A records only for the requested host or its explicit CNAME chain.
    for _ in range(len(answers)):
        for answer in answers:
            name = str(answer.get("name", "")).lower().rstrip(".")
            if answer.get("type") == 5 and name in names:
                names.add(str(answer.get("data", "")).lower().rstrip("."))
    addresses = set()
    for answer in answers:
        if answer.get("type") != 1 or str(answer.get("name", "")).lower().rstrip(".") not in names:
            continue
        try:
            address = ipaddress.IPv4Address(answer.get("data", ""))
        except ipaddress.AddressValueError:
            continue
        if address.is_global:
            addresses.add(str(address))
    return sorted(addresses)


def resolve_entries(session: requests.Session) -> list[str]:
    for resolver in RESOLVERS:
        try:
            response = session.get(resolver, params={"name": HOST, "type": "A"},
                                   headers={"Accept": "application/dns-json"}, timeout=10)
            response.raise_for_status()
            addresses = public_addresses(response.json())
            if addresses:
                # One fresh address avoids OS-dependent hosts-file ordering.
                return [f"{addresses[0]} {HOST}"]
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            print(f"HTTPS DNS lookup failed: {resolver}: {exc}", file=sys.stderr)
    return []


if __name__ == "__main__":
    with requests.Session() as session:
        for entry in resolve_entries(session):
            print(entry)
