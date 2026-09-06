from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from scripts import check_product_readiness as readiness
from scripts import generate_deployment_evidence as generation
from scripts import review_deployment_evidence as review
from scripts import verify_production_deployment as collection


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]


class DeploymentOriginTests(unittest.TestCase):
    normalizers = (
        collection._validated_public_origin,
        generation._canonical_origin,
        review._canonical_origin,
        readiness._canonical_deployment_origin,
    )

    def test_all_stages_preserve_the_same_valid_https_origin(self):
        cases = (
            ("https://MARKETS.EXAMPLE.NET:443/", "https://markets.example.net"),
            ("https://MARKETS.EXAMPLE.NET./", "https://markets.example.net"),
            ("https://caf\u00e9.example.net", "https://xn--caf-dma.example.net"),
            ("https://markets.example.net:8443", "https://markets.example.net:8443"),
            ("https://1.1.1.1/", "https://1.1.1.1"),
            ("https://[2606:4700:4700:0:0:0:0:1111]:443/", "https://[2606:4700:4700::1111]"),
            ("https://[2606:4700:4700::1111]:8443", "https://[2606:4700:4700::1111]:8443"),
        )
        for normalize in self.normalizers:
            for value, expected in cases:
                with self.subTest(stage=normalize.__module__, value=value):
                    with patch("scripts.verify_production_deployment.socket.getaddrinfo", return_value=PUBLIC_DNS):
                        self.assertEqual(normalize(value), expected)

    def test_all_stages_reject_ambiguous_or_non_origin_urls(self):
        invalid = (
            "https://@markets.example.net", "https://:@markets.example.net",
            "https://markets.example.net:0", "https://markets.example.net:",
            "https://markets.example.net:65536", "https://markets.example.net:bad",
            "https://markets.example.net/?", "https://markets.example.net/#",
            "https://markets.example.net/;params", "https://mar\nkets.example.net",
            "https://mar\tkets.example.net", "https://markets.example.net\\suffix",
            "https://[2606:4700:4700::1111%25interface]", "https://[broken",
            "https://[v1.fe80]",
            "https://%61nalytics.example.net", "https://bad..example.net",
            "https://-bad.example.net", "https://bad-.example.net",
            "http://markets.example.net", "https://markets.example.net/path",
        )
        for normalize in self.normalizers:
            for value in invalid:
                with self.subTest(stage=normalize.__module__, value=value):
                    with patch("scripts.verify_production_deployment.socket.getaddrinfo", return_value=PUBLIC_DNS):
                        if normalize is readiness._canonical_deployment_origin:
                            self.assertEqual(normalize(value), "")
                        else:
                            with self.assertRaises(ValueError):
                                normalize(value)

    def test_fixture_looking_hostnames_cannot_bypass_dns_validation(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
        with patch("scripts.verify_production_deployment.socket.getaddrinfo", return_value=private) as resolver:
            with self.assertRaises(ValueError):
                collection._validated_public_origin("https://analytics.example.com")
        resolver.assert_called_once()

    def test_mixed_private_and_public_dns_answers_are_rejected(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))]
        with patch("scripts.verify_production_deployment.socket.getaddrinfo", return_value=PUBLIC_DNS + private):
            with self.assertRaises(ValueError):
                collection._validated_public_origin("https://markets.example.net")

    def test_unresolved_public_hostnames_are_rejected(self):
        for answers in ([], socket.gaierror("unavailable")):
            with self.subTest(answers=answers):
                kwargs = {"side_effect": answers} if isinstance(answers, Exception) else {"return_value": answers}
                with patch("scripts.verify_production_deployment.socket.getaddrinfo", **kwargs):
                    with self.assertRaises(ValueError):
                        collection._validated_public_origin("https://markets.example.net")


if __name__ == "__main__":
    unittest.main()
