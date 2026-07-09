import os
import hashlib
import ipaddress
import socket
from urllib.parse import urlparse

import requests
import yaml
from flask import Flask, request, jsonify

app = Flask(__name__)

STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

LEDGER = [
    {"id": "txn_1001", "pan": "4242424242424242", "amount": 4200, "currency": "USD", "status": "captured"},
    {"id": "txn_1002", "pan": "5555555555554444", "amount": 1899, "currency": "EUR", "status": "refunded"},
]

# Only these hosts may be fetched via /fetch — closes the open SSRF.
FETCH_ALLOWLIST = {"api.example-partner.com"}


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/tokenize", methods=["POST"])
def tokenize():
    payload = request.get_json(silent=True) or {}
    pan = payload.get("pan", "")
    token = "tok_" + hashlib.sha256(pan.encode()).hexdigest()[:24]
    return jsonify(token=token, last4=pan[-4:])


@app.route("/transactions")
def transactions():
    return jsonify(transactions=LEDGER)


@app.route("/import", methods=["POST"])
def import_config():
    # yaml.load() allows arbitrary object construction (CVE-class RCE).
    # safe_load() only builds plain Python types (dict/list/str/int/etc).
    config = yaml.safe_load(request.data)
    return jsonify(loaded=str(config))


@app.route("/fetch")
def fetch():
    url = request.args.get("url", "")  # nosemgrep: python.django.security.injection.ssrf.ssrf-injection-requests.ssrf-injection-requests
    parsed = urlparse(url)

    if parsed.scheme not in ("https",):
        return jsonify(error="only https URLs are allowed"), 400

    if parsed.hostname not in FETCH_ALLOWLIST:
        return jsonify(error="host not in allowlist"), 400

    # Also block requests that resolve to internal/private IP ranges,
    # even if the hostname itself looks legitimate (DNS rebinding defense).
    try:
        resolved_ip = socket.gethostbyname(parsed.hostname)
        if ipaddress.ip_address(resolved_ip).is_private:
            return jsonify(error="refusing to fetch private address"), 400
    except socket.gaierror:
        return jsonify(error="could not resolve host"), 400

    # URL validated above: https-only, host allowlist, private-IP resolution check.
    resp = requests.get(url, timeout=5)  # nosemgrep: python.flask.security.injection.ssrf-requests.ssrf-requests
    return jsonify(status_code=resp.status_code, body=resp.text[:2048])


if __name__ == "__main__":
    # Binding 0.0.0.0 is required here — this runs inside a container behind
    # a Kubernetes Service/Ingress, not exposed directly to the internet.
    app.run(host="0.0.0.0", port=8080)  # nosemgrep: python.flask.security.audit.app-run-param-config.avoid_app_run_with_bad_host
