"""Project-local CA/leaf lifecycle. Never installs trust or touches system Nginx."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
from urllib.parse import urlsplit
from .capacity import ensure_space
from .config import CONFIG, DATA, PORTS

ROOT = DATA / "tls"
CA_DAYS = 3650
SERVER_DAYS = 365


def canonical_origin():
    value = CONFIG["tls"]["canonical_origin"]
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or parsed.port != PORTS["https"]
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("tls.canonical_origin must be https://127.0.0.1:<ports.https>")
    if CONFIG["auth"]["cookie_mode"] != "https" or CONFIG["auth"]["allowed_origins"] != [value]:
        raise ValueError("TLS requires https cookie mode and the single canonical allowed origin")
    return value


def paths():
    return {"ca_key": ROOT / "ca.key.pem", "ca_cert": ROOT / "ca.crt.pem",
            "server_key": ROOT / "server.key.pem", "server_cert": ROOT / "server.crt.pem"}


def openssl(*args):
    result = subprocess.run(["openssl", *map(str, args)], capture_output=True, text=True, umask=0o077, timeout=30)
    if result.returncode:
        raise ValueError("Local TLS certificate/key validation or issuance failed; inspect paths with python -m mtw.tls check")
    return result.stdout.strip()


def private(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError("TLS private key must be an owned native file with mode 0600")


def validate():
    canonical_origin()
    p = paths()
    if ROOT.is_symlink() or not ROOT.is_dir() or ROOT.stat().st_mode & 0o077:
        raise ValueError("Missing/private TLS directory required; run bash scripts/setup-https")
    if any(not p[name].is_file() or p[name].is_symlink() for name in ("ca_cert", "server_cert", "server_key")):
        raise ValueError("TLS certificate/key missing; run bash scripts/setup-https (no HTTP fallback)")
    private(p["server_key"])
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(p["server_cert"], p["server_key"], password=lambda: b"")
        openssl("verify", "-CAfile", p["ca_cert"], "-purpose", "sslserver", "-verify_ip", "127.0.0.1", p["server_cert"])
        openssl("verify", "-CAfile", p["ca_cert"], "-purpose", "sslserver", "-verify_hostname", "localhost", p["server_cert"])
        openssl("x509", "-in", p["server_cert"], "-noout", "-checkend", "86400")
    except (ssl.SSLError, OSError, ValueError) as error:
        raise ValueError("Invalid/expired/mismatched TLS certificate; restore it or run bash scripts/setup-https --renew-server with the original CA") from None
    return {"valid": True, "url": canonical_origin(), "ca_certificate": str(p["ca_cert"]),
            "server_certificate": str(p["server_cert"]),
            "ca_certificate_windows": str(ROOT / "ca.crt.cer"),
            "ca_sha256": openssl("x509", "-in", p["ca_cert"], "-noout", "-fingerprint", "-sha256").split("=", 1)[1],
            "server_dates": openssl("x509", "-in", p["server_cert"], "-noout", "-dates").splitlines(),
            "ca_dates": openssl("x509", "-in", p["ca_cert"], "-noout", "-dates").splitlines(),
            "san": openssl("x509", "-in", p["server_cert"], "-noout", "-ext", "subjectAltName"),
            "system_trust_modified": False}


def setup(renew_server=False):
    ensure_space()
    canonical_origin()
    ROOT.mkdir(mode=0o700, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_mode & 0o077:
        raise ValueError("TLS directory must be private (0700)")
    with (ROOT / ".setup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        p = paths()
        existing_ca = [p[name].exists() for name in ("ca_cert", "ca_key")]
        if any(existing_ca) and not all(existing_ca):
            raise ValueError("Incomplete local CA: restore the original pair; refusing to replace trust identity")
        if not any(existing_ca) and any(p[name].exists() for name in ("server_cert", "server_key")):
            raise ValueError("Leaf exists without CA; restore original CA, no automatic replacement")
        new_ca = not any(existing_ca)
        if not new_ca:
            private(p["ca_key"])
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(p["ca_cert"], p["ca_key"], password=lambda: b"")
                openssl("verify", "-CAfile", p["ca_cert"], p["ca_cert"])
            except (ssl.SSLError, OSError, ValueError):
                raise ValueError("Invalid original CA pair; restore it rather than generating a new CA") from None
        issue = new_ca or renew_server or not any(p[name].exists() for name in ("server_cert", "server_key"))
        if issue:
            with tempfile.TemporaryDirectory(prefix="issue-", dir=ROOT) as temporary:
                temp = Path(temporary)
                ca_key, ca_cert = p["ca_key"], p["ca_cert"]
                if new_ca:
                    ca_key, ca_cert = temp / "ca.key.pem", temp / "ca.crt.pem"
                    openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", CA_DAYS,
                        "-keyout", ca_key, "-out", ca_cert, "-subj", "/CN=MonoTrajectoryWeb Local Development CA",
                        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
                key, csr, cert = temp / "server.key.pem", temp / "server.csr.pem", temp / "server.crt.pem"
                openssl("req", "-new", "-newkey", "rsa:3072", "-nodes", "-keyout", key, "-out", csr,
                        "-subj", "/CN=localhost")
                extension = temp / "server.ext"
                extension.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
                openssl("x509", "-req", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key,
                        "-set_serial", "0x" + secrets.token_hex(16), "-out", cert, "-days", SERVER_DAYS,
                        "-sha256", "-extfile", extension)
                if new_ca:
                    os.replace(ca_key, p["ca_key"]); os.replace(ca_cert, p["ca_cert"])
                os.replace(key, p["server_key"]); os.replace(cert, p["server_cert"])
        result = validate()
        windows_cert = ROOT / "ca.crt.cer"
        if windows_cert.is_symlink():
            raise ValueError("Public CA export must not be a symlink")
        encoded = ssl.PEM_cert_to_DER_cert(p["ca_cert"].read_text())
        if not windows_cert.exists() or windows_cert.read_bytes() != encoded:
            windows_cert.write_bytes(encoded)
        return result | {"new_ca": new_ca, "server_issued": issue}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "check"])
    parser.add_argument("--renew-server", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(setup(args.renew_server) if args.action == "setup" else validate(), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(2, "TLS configuration error: " + str(error) + "\n")


if __name__ == "__main__":
    main()
