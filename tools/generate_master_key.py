"""Print a new 32-byte master key. Put it in MASTER_ENCRYPTION_KEY and do not lose it."""

import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode())
