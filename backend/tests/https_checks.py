"""Real curl CA/hostname checks and isolated HTTPS auth; no SLAM or trust install."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import ssl
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
from mtw import tls
from mtw.capacity import ensure_space
from mtw.config import DATA
from mtw.files import atomic_json, read_json
from mtw.service_auth import service_headers


def run(fixture=None):
    ensure_space()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "checks": {}, "requests": [], "passed": False}
    ca = tls.paths()["ca_cert"]
    base = tls.canonical_origin()
    with tempfile.TemporaryDirectory(prefix="https-curl-", dir=DATA / "reports") as temporary:
        root = Path(temporary)
        sequence = 0
        def request(label, url, expected=200, method="GET", headers=None, data=None, jar=None, flags=(), trust=True):
            nonlocal sequence
            assert urlsplit(url).hostname in {"127.0.0.1", "localhost"}
            sequence += 1
            body, response_headers = root / f"{sequence}.body", root / f"{sequence}.headers"
            config = ["url = " + json.dumps(url), "request = " + json.dumps(method)]
            config.extend("header = " + json.dumps(f"{k}: {v}") for k, v in (headers or {}).items())
            if data is not None:
                config += ['header = "Content-Type: application/json"', "data = " + json.dumps(json.dumps(data))]
            # Credentials/CSRF only enter curl via stdin, never argv/logs.
            argv = ["curl", "--config", "-", "--silent", "--show-error", "--noproxy", "*", "--max-time", "10",
                    "--output", str(body), "--dump-header", str(response_headers), "--write-out", "%{http_code} %{ssl_verify_result}"]
            if trust:
                argv += ["--cacert", str(ca)]
            if jar:
                argv += ["--cookie", str(jar), "--cookie-jar", str(jar)]
            argv += list(flags)
            result = subprocess.run(argv, input="\n".join(config) + "\n", capture_output=True, text=True, timeout=15, umask=0o077)
            if not trust and result.returncode:
                report["requests"].append({"label": label, "url": url, "curl_exit": result.returncode})
                return result.returncode
            assert result.returncode == 0, (label, result.returncode, result.stderr[:180])
            status, verified = map(int, result.stdout.split())
            assert status == expected, (label, status, expected)
            if url.startswith("https:"):
                assert verified == 0
            values = response_headers.read_text().lower()
            cookie_flags = {"secure": "; secure" in values, "httponly": "; httponly" in values,
                            "same_site_strict": "samesite=strict" in values} if "set-cookie:" in values else None
            report["requests"].append({"label": label, "method": method, "url": url, "http": status,
                                       "ssl_verify_result": verified, "cookie_flags": cookie_flags})
            return body.read_bytes(), values, cookie_flags

        request("ip_chain_hostname", base + "/health")
        request("localhost_chain_hostname", "https://localhost:8443/health", expected=308)
        request("tls12", base + "/health", flags=("--tlsv1.2", "--tls-max", "1.2"))
        request("tls13", base + "/health", flags=("--tlsv1.3", "--tls-max", "1.3"))
        no_ca = request("without_project_ca", base + "/health", trust=False)
        report["default_curl_trust"] = no_ca == 0 if isinstance(no_ca, int) else True
        _, headers, _ = request("redirect_path_query", "http://127.0.0.1:8080/api/jobs?page=2&page_size=20", expected=308)
        assert "location: " + base + "/api/jobs?page=2&page_size=20" in headers
        request("http_mutation_denied", "http://127.0.0.1:8080/api/auth/login", expected=426, method="POST", data={})
        request("private_http_denied", "http://127.0.0.1:8000/api/auth/session", expected=400)
        request("spoofed_forwarding_denied", "http://127.0.0.1:8000/api/auth/session", expected=400,
                headers={"Host": "127.0.0.1:8443", "X-Forwarded-Host": "127.0.0.1:8443", "X-Forwarded-Proto": "https", "X-MTW-Proxy-Key": "wrong"})
        request("https_anonymous", base + "/api/jobs", expected=401)
        for version, protocol in ((ssl.TLSVersion.TLSv1_2, "TLSv1.2"), (ssl.TLSVersion.TLSv1_3, "TLSv1.3")):
            context = ssl.create_default_context(cafile=ca)
            context.minimum_version = context.maximum_version = version
            with socket.create_connection(("127.0.0.1", 8443), timeout=5) as peer:
                with context.wrap_socket(peer, server_hostname="127.0.0.1") as secure:
                    assert secure.version() == protocol
        legacy = subprocess.run(["openssl", "s_client", "-connect", "127.0.0.1:8443", "-tls1_1",
                                 "-cipher", "DEFAULT:@SECLEVEL=0"], input="", text=True, capture_output=True, timeout=10)
        report["tls11_rejected_by_peer"] = "alert protocol version" in legacy.stderr.lower()
        assert report["tls11_rejected_by_peer"]
        report["checks"]["ca_hostname_tls12_tls13_redirect_no_http_bypass"] = True

        before = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in tls.paths().items()}
        repeated = tls.setup()
        after = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in tls.paths().items()}
        assert before == after and not repeated["new_ca"] and not repeated["server_issued"]
        report["checks"]["repeat_setup_keeps_ca_and_leaf"] = True
        report["certificate"] = {k: repeated[k] for k in ("url", "ca_sha256", "server_dates", "ca_dates", "san")}

        if fixture:
            fixture = Path(fixture).resolve()
            assert fixture.parent == (DATA / "reports").resolve() and re.fullmatch(r"auth-browser-[0-9a-f]{32}", fixture.name)
            metadata = read_json(fixture / "credentials.json")
            test_base = metadata["base"]
            assert test_base == "https://127.0.0.1:8444"
            jar = root / "cookies"
            def bootstrap():
                body, _, cookie = request("auth_bootstrap", test_base + "/api/auth/session", jar=jar)
                assert cookie and all(cookie.values())
                return json.loads(body)["csrf_token"]
            def mutation(path, csrf, data=None, expected=200, origin=test_base):
                return request(path.rsplit("/", 1)[-1], test_base + path, method="POST", expected=expected,
                               headers={"Origin": origin, "X-CSRF-Token": csrf}, data=data, jar=jar)
            csrf = bootstrap()
            credentials = {"login": "tls_" + secrets.token_hex(6), "password": secrets.token_urlsafe(32)}
            body, _, cookie = mutation("/api/auth/register", csrf, credentials, 201)
            assert all(cookie.values())
            current = json.loads(body)
            csrf = current["csrf_token"]
            user_id = current["user"]["id"]
            body, _, _ = request("me", test_base + "/api/auth/me", jar=jar)
            assert json.loads(body)["id"] == user_id
            mutation("/api/auth/logout", "", expected=403)
            mutation("/api/auth/logout", csrf, expected=403, origin="http://127.0.0.1:8180")
            mutation("/api/auth/logout", csrf, expected=204)
            request("logged_out", test_base + "/api/auth/me", jar=jar, expected=401)
            csrf = bootstrap()
            mutation("/api/auth/login", csrf, credentials | {"password": secrets.token_urlsafe(32)}, 401)
            body, _, cookie = mutation("/api/auth/login", csrf, credentials)
            assert all(cookie.values())
            mutation("/api/auth/logout", json.loads(body)["csrf_token"], expected=204)
            csrf = bootstrap()
            owner = metadata["users"]["a"]
            owner_password = hmac.new((fixture / "fixture-seed").read_bytes(), owner["login"].encode(), hashlib.sha256).hexdigest()
            body, _, _ = mutation("/api/auth/login", csrf, {"login": owner["login"], "password": owner_password})
            csrf = json.loads(body)["csrf_token"]
            job = "/api/jobs/" + owner["job_id"]
            request("own_state", test_base + job, jar=jar)
            body, _, _ = request("own_snapshot", test_base + job + "/snapshot", jar=jar)
            frame_id = json.loads(body)["frame_id"]
            request("own_preview", test_base + job + f"/frames/{frame_id}", jar=jar)
            request("own_video_range", test_base + job + "/video", jar=jar, expected=206, headers={"Range": "bytes=0-63"})
            request("own_result", test_base + job + "/result", jar=jar)
            request("own_artifact", test_base + job + "/artifacts/native-summary.json", jar=jar)
            foreign = "/api/jobs/" + metadata["users"]["b"]["job_id"]
            request("foreign_state", test_base + foreign, jar=jar, expected=404)
            mutation(foreign + "/cancel", csrf, expected=404)
            mutation("/api/auth/logout", csrf, expected=204)
            report["checks"]["curl_https_auth_secure_csrf_owner_files"] = True

        with httpx.Client(trust_env=False, timeout=5) as client:
            marker = str(uuid4())
            invalid = {"job_id": marker, "dispatch_token": str(uuid4())}
            assert client.post("http://127.0.0.1:8081/execute", json=invalid).status_code == 401
            for port in (8081, 8101, 8102):
                assert client.get(f"http://127.0.0.1:{port}/health", headers=service_headers()).status_code == 200
            assert client.post("http://127.0.0.1:8081/execute", json=invalid,
                               headers=service_headers() | {"X-MTW-Job": marker}).status_code == 409
            report["service_probe_job_marker"] = marker
            report["checks"]["private_service_key_path_still_works_no_worker_started"] = True
        report["passed"] = True
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(DATA / "reports/prompt9-curl-checks.json", report)
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "requests": len(report["requests"]),
                      "default_curl_trust": report["default_curl_trust"], "tls11_rejected": report["tls11_rejected_by_peer"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture")
    run(parser.parse_args().fixture)
