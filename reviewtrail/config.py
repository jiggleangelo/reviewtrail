import hashlib
import hmac
import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VAULT_DIR = os.path.join(os.path.expanduser('~'), '.local', 'state', 'reviewtrail', 'vault')


def _local_value(environment_name, filename):
    configured = os.getenv(environment_name)
    path = Path(configured or os.path.join(os.path.expanduser('~'), '.local', 'state', 'reviewtrail', filename))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(value + '\n')
    os.chmod(path, 0o600)
    return value


def _local_secret():
    return _local_value('REVIEWTRAIL_SECRET_FILE', 'secret')


def _local_vault_key():
    return _local_value('REVIEWTRAIL_VAULT_KEY_FILE', 'vault-key')


@dataclass
class Config:
    secret: str
    db_path: str
    vault_dir: str
    oauth_mode: str
    github_client_id: str
    github_client_secret: str
    dpo_contact: str
    env: str
    privacy_approved: bool
    https: bool
    retention_days: int
    trusted_proxy_header: str = ''
    vault_explicit: bool = True
    trusted_proxy_hops: int = 1
    vault_key: str = ''

    @classmethod
    def from_env(cls):
        c = cls(
            os.getenv('SECRET_KEY') or _local_secret(),
            os.getenv('DB_PATH', 'reviewtrail.db'),
            os.getenv('VAULT_DIR', DEFAULT_VAULT_DIR),
            os.getenv('OAUTH_MODE', 'fake'),
            os.getenv('GITHUB_CLIENT_ID', ''),
            os.getenv('GITHUB_CLIENT_SECRET', ''),
            os.getenv('DPO_CONTACT', ''),
            os.getenv('ENV', 'dev'),
            os.getenv('PRIVACY_APPROVED') == '1',
            os.getenv('HTTPS') == '1',
            int(os.getenv('RETENTION_DAYS', '365')),
            os.getenv('TRUSTED_PROXY_HEADER', ''),
            'VAULT_DIR' in os.environ,
            int(os.getenv('TRUSTED_PROXY_HOPS', '1')),
            os.getenv('VAULT_KEY') or _local_vault_key(),
        )
        c.check()
        return c

    def check(self):
        if len(self.secret.encode()) < 32:
            raise RuntimeError('SECRET_KEY must contain at least 32 bytes')
        if self.trusted_proxy_hops < 1:
            raise RuntimeError('TRUSTED_PROXY_HOPS must be at least 1')
        if self.oauth_mode not in {'fake', 'github'}:
            raise RuntimeError('OAUTH_MODE must be fake or github')
        if self.retention_days < 1:
            raise RuntimeError('RETENTION_DAYS must be positive')
        if self.env != 'prod':
            return
        if not self.privacy_approved or not self.dpo_contact:
            raise RuntimeError('Production requires PRIVACY_APPROVED=1 and DPO_CONTACT')
        if self.oauth_mode == 'fake':
            raise RuntimeError('OAUTH_MODE=fake is forbidden in production')
        if not self.vault_explicit:
            raise RuntimeError('Production requires an explicit, persistent VAULT_DIR')
        if 'VAULT_KEY' not in os.environ:
            raise RuntimeError('Production requires a separate, persistent VAULT_KEY')
        if not self.https:
            raise RuntimeError('Production requires HTTPS=1')
        if not self.trusted_proxy_header:
            raise RuntimeError('Production requires TRUSTED_PROXY_HEADER and a fixed TRUSTED_PROXY_HOPS value')

    def assert_request_allowed(self, remote_address):
        if self.oauth_mode != 'fake':
            return
        try:
            address = ipaddress.ip_address(remote_address)
        except ValueError as error:
            raise RuntimeError('OAUTH_MODE=fake accepts loopback requests only') from error
        if not address.is_loopback:
            raise RuntimeError('OAUTH_MODE=fake accepts loopback requests only')

    def key(self, purpose):
        return hmac.new(self.secret.encode(), ('reviewtrail:' + purpose).encode(), hashlib.sha256).hexdigest()

    def effective_vault_key(self):
        if self.vault_key:
            return self.vault_key
        if self.env == 'prod':
            raise RuntimeError('A separate VAULT_KEY is required')
        return self.key('development-vault')
