"""Step 1/3: secret key handling, keyed hashing and encrypted mapping files.

One 32-byte master secret (kept in a key file that should live on separate,
access-controlled storage, e.g. an encrypted USB) derives two sub-keys:
  * a Fernet key that encrypts every identity mapping written to the vault;
  * an HMAC key used for all keyed hashes (PIK parts, file UIDs, ID hashes).

Using HMAC instead of a plain hash is what stops a dictionary attack of the
form "hash every plausible Bangladeshi name + birth year and compare".
"""
import base64
import getpass
import hashlib
import hmac
import json
import platform
import secrets
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, secure_dir: Path, key_file: Path):
        self.dir = Path(secure_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        key_file = Path(key_file)
        self.created_key = False
        if key_file.exists():
            master = base64.b64decode(key_file.read_text().strip())
        else:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            master = secrets.token_bytes(32)
            key_file.write_text(base64.b64encode(master).decode())
            self.created_key = True
        if len(master) != 32:
            raise ValueError(f"master key in {key_file} is not 32 bytes")
        self._fernet = Fernet(base64.urlsafe_b64encode(self._derive(master, b"fernet-v1")))
        self._link_key = self._derive(master, b"ksrl-link-v1")

    @staticmethod
    def _derive(master: bytes, label: bytes) -> bytes:
        return hmac.new(master, label, hashlib.sha256).digest()

    def keyed_hash(self, domain: str, value: str, n: int = 32) -> str:
        msg = f"{domain}\x1f{value}".encode("utf-8")
        return hmac.new(self._link_key, msg, hashlib.sha256).hexdigest()[:n]

    def save(self, name: str, obj) -> None:
        path = self.dir / f"{name}.json.enc"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8")))
        tmp.replace(path)

    def load(self, name: str, default):
        path = self.dir / f"{name}.json.enc"
        if not path.exists():
            return default
        return json.loads(self._fernet.decrypt(path.read_bytes()).decode("utf-8"))

    def log_access(self, command: str, **counts) -> None:
        """Append-only access log (who ran what, when). Never contains PHI."""
        try:
            user = getpass.getuser()
        except Exception:
            user = "unknown"
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "user": user,
                 "host": platform.node(), "command": command, **counts}
        with open(self.dir / "access_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
