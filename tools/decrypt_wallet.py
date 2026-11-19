"""Print a user's wallet secret on this PC only.

Usage:
    python tools/decrypt_wallet.py <telegram_id>

Requires TURSO_* and MASTER_ENCRYPTION_KEY in .env.
Does not talk to Telegram. Do not paste the output into a chat.
"""

import sys

import base58
from solders.keypair import Keypair

from sniper.config import Config
from sniper.crypto import EnvelopeCipher
from sniper.db import Database
from sniper.errors import ConfigError


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/decrypt_wallet.py <telegram_id>")
    try:
        cfg = Config.from_env("decrypt")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    telegram_id = sys.argv[1].strip()
    db = Database.connect(cfg)
    try:
        payload = db.get_secret_payload_sync(telegram_id)
        user = db.get_user_sync(telegram_id)
    finally:
        db.close()
    if not user or not payload:
        raise SystemExit("No encrypted wallet for that Telegram id.")
    secret = EnvelopeCipher(cfg.master_key).decrypt_json(payload)
    keypair = Keypair.from_bytes(secret)
    print("WARNING: the next lines are a live private key. Anyone who sees them can spend the wallet.", file=sys.stderr)
    print(f"address={keypair.pubkey()}")
    print(f"stored_address={user['wallet_address']}")
    print(f"private_key_base58={base58.b58encode(bytes(keypair)).decode()}")
    if str(keypair.pubkey()) != user["wallet_address"]:
        print("WARNING: decrypted key does not match the stored address.", file=sys.stderr)


if __name__ == "__main__":
    main()
