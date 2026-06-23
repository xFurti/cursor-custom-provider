#!/usr/bin/env python3
"""Validation script for cursor-custom-provider.

Run without arguments to validate config.json:
    python test.py

Run with --live to also start the proxy and test authentication:
    python test.py --live
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# Force UTF-8 output on Windows so emojis and box-drawing chars print correctly
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
REQUIRED_KEYS = ["port", "target_base_url", "api_key", "model_prefix", "proxy_secret"]


def check(description, condition):
    symbol = "✅" if condition else "❌"
    print(f"{symbol} {description}")
    return condition


def validate_config():
    print("\n=== Configuration validation ===")
    if not os.path.exists(CONFIG_PATH):
        check("config.json exists", False)
        return False

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        check(f"config.json is valid JSON ({e})", False)
        return False

    all_ok = True
    for key in REQUIRED_KEYS:
        value = config.get(key, "")
        present = bool(value) and str(value).strip() != ""
        all_ok &= check(f"'{key}' is set", present)

    port = config.get("port", 0)
    all_ok &= check("'port' is a number", isinstance(port, int) and 1 <= port <= 65535)

    tunnel = config.get("tunnel_provider", "ngrok").lower()
    if tunnel == "ngrok":
        all_ok &= check("'ngrok_authtoken' is set for ngrok", bool(config.get("ngrok_authtoken", "")))

    return all_ok


def test_live():
    print("\n=== Live proxy test ===")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    port = config["port"]
    secret = config["proxy_secret"]

    proc = subprocess.Popen(
        [sys.executable, "cursor_proxy.py"],
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for server to start (max 15 seconds)
    started = False
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=1)
            started = True
            break
        except urllib.error.HTTPError:
            started = True
            break
        except Exception:
            continue

    if not started:
        proc.terminate()
        proc.wait(timeout=5)
        check("Proxy server started", False)
        return False

    all_ok = True

    # Test 1: no auth -> 401
    try:
        urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=5)
        all_ok &= check("Unauthenticated request rejected with 401", False)
    except urllib.error.HTTPError as e:
        all_ok &= check("Unauthenticated request rejected with 401", e.code == 401)
    except Exception as e:
        all_ok &= check(f"Unauthenticated request test failed ({e})", False)

    # Test 2: wrong auth -> 401
    req = urllib.request.Request(
        f"http://localhost:{port}/v1/models",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        all_ok &= check("Wrong secret rejected with 401", False)
    except urllib.error.HTTPError as e:
        all_ok &= check("Wrong secret rejected with 401", e.code == 401)
    except Exception as e:
        all_ok &= check(f"Wrong secret test failed ({e})", False)

    # Test 3: correct auth -> passes proxy auth (provider may still error)
    req = urllib.request.Request(
        f"http://localhost:{port}/v1/models",
        headers={"Authorization": f"Bearer {secret}"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        all_ok &= check("Correct secret passes proxy auth", True)
    except urllib.error.HTTPError as e:
        # Any status other than 401 means auth passed; provider returned its own error.
        auth_ok = e.code != 401
        all_ok &= check(f"Correct secret passes proxy auth (provider returned {e.code})", auth_ok)
    except Exception as e:
        all_ok &= check(f"Correct secret test failed ({e})", False)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    return all_ok


def main():
    print("cursor-custom-provider validation")
    print("=================================")

    config_ok = validate_config()
    live_ok = True

    if "--live" in sys.argv:
        live_ok = test_live()
    else:
        print("\nℹ️  Pass --live to also start the proxy and test authentication.")

    print("\n=== Result ===")
    if config_ok and live_ok:
        print("✅ All checks passed.")
        sys.exit(0)
    else:
        print("❌ Some checks failed. Review the messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
