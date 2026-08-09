#!/usr/bin/env python3
"""Merge Sendblue credentials into the SatGPT config and switch the provider.

Interactive: run it with no arguments and it prompts for each value — just
paste and press Enter. Only the sendblue_* keys (+ sms_provider) are touched;
every other key is left as-is. Writes a timestamped .bak first. The API secret
is read via a hidden prompt so it never lands in shell history.

Usage (on the droplet):
    python3 set_sendblue.py                 # asks for everything
    python3 set_sendblue.py --restart       # asks, then restarts the service

Anything you'd rather not be asked can be passed as a flag instead:
    --config /root/.satrelay/sms.json  --key-id <sb-api-key-id>
    --secret <sb-api-key-secret>  --from-number +1...  --provider sendblue|twilio

Then point the Sendblue "receive" webhook at https://<host>/sendblue.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time


def _default_config():
    for p in ("/root/.satrelay/sms.json", os.path.expanduser("~/.satrelay/config.json")):
        if os.path.exists(p):
            return p
    return "/root/.satrelay/sms.json"


def main():
    ap = argparse.ArgumentParser(description="Add Sendblue keys to the SatGPT config.")
    ap.add_argument("--config", help="Path to the config JSON (else you'll be asked)")
    ap.add_argument("--key-id", help="sb-api-key-id (else you'll be asked)")
    ap.add_argument("--secret", help="sb-api-key-secret (else you'll be asked, hidden)")
    ap.add_argument("--from-number", help="Dedicated-line number; blank on a shared/trial line")
    ap.add_argument("--provider", default="sendblue", choices=["sendblue", "twilio"],
                    help="Sets sms_provider (default sendblue)")
    ap.add_argument("--restart", action="store_true",
                    help="Run `systemctl restart satgpt-sms` afterward")
    args = ap.parse_args()

    # Prompt for anything not passed on the command line — just paste values.
    if args.config:
        path = os.path.expanduser(args.config)
    else:
        d = _default_config()
        path = os.path.expanduser(input(f"Config path [{d}]: ").strip() or d)
    if not os.path.exists(path):
        sys.exit(f"Config not found: {path}")

    key_id = args.key_id or input("Sendblue API key id (sb-api-key-id): ").strip()
    if not key_id:
        sys.exit("No key id provided — aborting.")

    secret = args.secret or getpass.getpass("Sendblue API secret (hidden, paste + Enter): ").strip()
    if not secret:
        sys.exit("No secret provided — aborting.")

    if args.from_number is None:
        from_number = input("From number (blank = shared/trial line): ").strip()
    else:
        from_number = args.from_number

    with open(path) as f:
        cfg = json.load(f)          # fail loudly if the file is malformed

    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)

    cfg["sms_provider"] = args.provider
    cfg["sendblue_api_key_id"] = key_id
    cfg["sendblue_api_secret"] = secret
    cfg["sendblue_from_number"] = from_number
    cfg.setdefault("sendblue_secret_header", "sb-signing-secret")
    cfg.setdefault("sendblue_webhook_secret", "")
    cfg.setdefault("verify_sendblue_secret", False)

    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)           # atomic swap
    os.chmod(path, 0o600)           # secret inside — keep it owner-only

    print(f"\nUpdated {path}")
    print(f"  backup:        {backup}")
    print(f"  sms_provider:  {args.provider}")
    print(f"  key-id:        {key_id}")
    print(f"  from_number:   {from_number or '(shared/trial line)'}")
    print("  secret:        set (not shown)")

    if args.restart:
        print("\nRestarting satgpt-sms ...")
        subprocess.run(["systemctl", "restart", "satgpt-sms"], check=False)
        subprocess.run(["systemctl", "--no-pager", "status", "satgpt-sms"], check=False)
    else:
        print("\nNext: restart the service, then point Sendblue's receive webhook here:")
        print("  systemctl restart satgpt-sms")
        print("  Sendblue dashboard -> Webhooks -> receive -> https://<host>/sendblue")


if __name__ == "__main__":
    main()
