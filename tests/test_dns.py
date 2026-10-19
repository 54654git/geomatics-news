"""Keep DNS fallback scoped and reject unusable resolver answers."""
import unittest
from unittest.mock import Mock

from scripts import resolve_mnr as dns


class DNSTests(unittest.TestCase):
    def test_accepts_public_address_along_cname_chain_only(self):
        payload = {"Status": 0, "Answer": [
            {"name": dns.HOST + ".", "type": 5, "data": "cdn.example.net."},
            {"name": "cdn.example.net.", "type": 1, "data": "120.221.10.137"},
            {"name": "other.example.net.", "type": 1, "data": "8.8.8.8"},
            {"name": "cdn.example.net.", "type": 1, "data": "127.0.0.1"},
        ]}
        self.assertEqual(dns.public_addresses(payload), ["120.221.10.137"])

    def test_dns_error_and_private_addresses_produce_no_hosts_entry(self):
        self.assertEqual(dns.public_addresses({"Status": 3}), [])
        for value in ("10.0.0.1", "invalid", "169.254.169.254"):
            self.assertEqual(dns.public_addresses({"Status": 0, "Answer": [
                {"name": dns.HOST, "type": 1, "data": value}]}), [])

    def test_tries_second_resolver_when_first_has_no_answer(self):
        session = Mock()
        first, second = Mock(), Mock()
        first.json.return_value = {"Status": 2}
        second.json.return_value = {"Status": 0, "Answer": [
            {"name": dns.HOST, "type": 1, "data": "120.221.10.137"}]}
        session.get.side_effect = [first, second]
        self.assertEqual(dns.resolve_entries(session), [f"120.221.10.137 {dns.HOST}"])
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
