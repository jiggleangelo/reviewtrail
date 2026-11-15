import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from typing import Protocol


class Vault(Protocol):
    def put(self, kind: str, value: str) -> str: ...
    def digest(self, value: str) -> str: ...
    def forget(self, token: str) -> bool: ...


def _derive(key, purpose):
    raw = key.encode() if isinstance(key, str) else bytes(key)
    return hmac.new(raw, b'reviewtrail-vault-' + purpose, hashlib.sha256).digest()


def _stream(key, nonce, length):
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, nonce + counter.to_bytes(8, 'big'), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


class FileVault:
    """Write-only contact storage used by the web application.

    Decryption is deliberately absent from this module. The separate DPO export
    tool contains the offline recovery operation and must not be imported by the
    application. Records use an encrypt-then-MAC construction built solely from
    Python's standard-library cryptographic primitives.
    """

    def __init__(self, path, key, digest_key=None):
        self.path = os.path.abspath(path)
        self.lock_path = self.path + '.lock'
        self._encryption_key = _derive(key, b'encryption')
        self._authentication_key = _derive(key, b'authentication')
        self._digest_key = _derive(digest_key if digest_key is not None else key, b'digest')
        directory = os.path.dirname(self.path)
        if os.path.lexists(directory) and stat.S_ISLNK(os.lstat(directory).st_mode):
            raise RuntimeError('Vault directory must not be a symlink')
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.stat(directory).st_uid != os.getuid():
            raise RuntimeError('Vault directory must be owned by the service user')
        os.chmod(directory, 0o700)
        for candidate in (self.path, self.lock_path):
            if os.path.lexists(candidate) and stat.S_ISLNK(os.lstat(candidate).st_mode):
                raise RuntimeError('Vault files must not be symlinks')
            if os.path.exists(candidate) and os.stat(candidate).st_uid != os.getuid():
                raise RuntimeError('Vault files must be owned by the service user')

    def _locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _seal(self, value):
        nonce = secrets.token_bytes(32)
        plaintext = value.encode('utf-8')
        stream = _stream(self._encryption_key, nonce, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
        tag = hmac.new(
            self._authentication_key,
            b'reviewtrail-contact-v2\0' + nonce + ciphertext,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(nonce + ciphertext + tag).decode('ascii')

    def _append_unlocked(self, obj):
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.write(fd, (json.dumps(obj, separators=(',', ':')) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _replace_unlocked(self, objects):
        directory = os.path.dirname(self.path)
        fd, temporary = tempfile.mkstemp(prefix='.contacts-', dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                for obj in objects:
                    output.write(json.dumps(obj, separators=(',', ':')) + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def put(self, kind, value):
        token = secrets.token_urlsafe(24)
        lock = self._locked()
        try:
            self._append_unlocked({
                'token': token,
                'kind': kind,
                'ciphertext': self._seal(value),
                'format': 'stdlib-etm-v2',
                'ts': datetime.now(timezone.utc).isoformat(),
            })
        finally:
            os.close(lock)
        return token

    def digest(self, value):
        return hmac.new(self._digest_key, value.strip().lower().encode(), hashlib.sha256).hexdigest()

    def forget(self, token):
        lock = self._locked()
        try:
            retained = []
            found = False
            if os.path.exists(self.path):
                with open(self.path, encoding='utf-8') as source:
                    for line in source:
                        item = json.loads(line)
                        if item.get('token') == token:
                            found = True
                            continue
                        if item.get('tombstone') == token:
                            continue
                        retained.append(item)
            if found:
                retained.append({'tombstone': token, 'ts': datetime.now(timezone.utc).isoformat()})
                self._replace_unlocked(retained)
            return found
        finally:
            os.close(lock)


class MemoryVault:
    def __init__(self, key='test'):
        self.key = key.encode()
        self.items = []

    def put(self, kind, value):
        token = secrets.token_urlsafe(24)
        self.items.append({'token': token, 'kind': kind, 'value': value})
        return token

    def digest(self, value):
        return hmac.new(self.key, value.strip().lower().encode(), hashlib.sha256).hexdigest()

    def forget(self, token):
        before = len(self.items)
        self.items = [item for item in self.items if item.get('token') != token]
        return len(self.items) != before

    def _dump(self):
        return list(self.items)
