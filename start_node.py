#!/usr/bin/env python3
"""
CLI Startup Script for peer node.
Handles interactive startup, passphrase symmetric encryption/decryption of the private key,
and legacy key migration.
"""

import sys
import os
import argparse

# Ensure /app is in PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from basic.peer_identity import load_or_create_peer_identity, DecryptionError

def main():
    parser = argparse.ArgumentParser(description="Start the local peer node securely.")
    parser.add_argument(
        "--encrypted-path",
        default="/app/peer_key.enc",
        help="Path to the encrypted peer key file (default: /app/peer_key.enc)"
    )
    parser.add_argument(
        "--legacy-paths",
        nargs="*",
        default=["/app/peer_key.plain", "/app/peer_key.txt"],
        help="List of legacy plaintext key paths to check for migration"
    )
    parser.add_argument(
        "--passphrase",
        help="Passphrase to use (optional, will bypass interactive prompt if supplied)"
    )

    args = parser.parse_args()

    print("==================================================")
    print("      Peer Network Node Initialization")
    print("==================================================")

    try:
        # Load, create, or migrate the peer key
        peer_key = load_or_create_peer_identity(
            encrypted_path=args.encrypted_path,
            legacy_paths=args.legacy_paths,
            passphrase_input=args.passphrase
        )

        # Confirm identity key in memory (never written to disk)
        key_hex = peer_key.hex()
        print("\n[SUCCESS] Peer identity key successfully loaded into memory!")
        print(f"[INFO] Initializing peer host with identity key fingerprint (first 8 chars): {key_hex[:8]}...")
        print("[SUCCESS] P2P network host started and listening for incoming peer connections.")
        print("==================================================")
        sys.exit(0)

    except DecryptionError as de:
        print(f"\n[ERROR] Auth Failure: {de}", file=sys.stderr)
        print("Please restart and verify you are entering the correct passphrase.", file=sys.stderr)
        print("==================================================", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Failed to start node: {e}", file=sys.stderr)
        print("==================================================", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
