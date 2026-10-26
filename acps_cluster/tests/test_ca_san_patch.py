"""wit 2.1.0 CSR SAN 补丁：IP/DNS 端点领证不再被本地校验误杀。"""

from __future__ import annotations

import ipaddress
import os
import sys
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.core import cluster_common  # noqa: E402,F401  (导入即打补丁)


class CaCsrSanPatchTest(unittest.TestCase):
    def _csr(self, host: str, kind: str):
        old_host = os.environ.get("ACPS_PUBLIC_HOST")
        old_kind = os.environ.get("ACPS_CERT_SAN_KIND")
        os.environ["ACPS_PUBLIC_HOST"] = host
        os.environ["ACPS_CERT_SAN_KIND"] = kind
        try:
            from wit.acps import ca

            key = ec.generate_private_key(ec.SECP256R1())
            return x509.load_pem_x509_csr(ca._generate_csr(key, "1.2.3.4.5"))
        finally:
            if old_host is None:
                os.environ.pop("ACPS_PUBLIC_HOST", None)
            else:
                os.environ["ACPS_PUBLIC_HOST"] = old_host
            if old_kind is None:
                os.environ.pop("ACPS_CERT_SAN_KIND", None)
            else:
                os.environ["ACPS_CERT_SAN_KIND"] = old_kind

    def test_ip端点写入IP_SAN(self):
        csr = self._csr("203.0.113.7", "ip")
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn(
            ipaddress.ip_address("203.0.113.7"),
            san.get_values_for_type(x509.IPAddress),
        )

    def test_dns端点写入DNS_SAN(self):
        csr = self._csr("agent.example.test", "dns")
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn("agent.example.test", san.get_values_for_type(x509.DNSName))


if __name__ == "__main__":
    unittest.main()
