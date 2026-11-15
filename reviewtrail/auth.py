import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass
class Identity:
    subject: str
    login: str
    created_at: date


class OAuthError(Exception):
    pass


class FakeOAuth:
    users = {
        'alice-old': Identity('1001', 'alice', date(2020, 1, 2)),
        'bob-new': Identity('1002', 'bob', datetime.now(timezone.utc).date()),
    }

    def authorize_url(self, state):
        return '/oauth/callback?code=alice-old&state=' + urllib.parse.quote(state)

    def exchange(self, code, state):
        if not state:
            raise OAuthError('Missing OAuth state')
        try:
            return self.users[code]
        except KeyError as error:
            raise OAuthError('Invalid authorization code') from error


class GitHubOAuth:
    def __init__(self, client_id, client_secret, opener=urllib.request.urlopen, timeout=10):
        self.client_id = client_id
        self.client_secret = client_secret
        self.opener = opener
        self.timeout = timeout

    def authorize_url(self, state):
        return 'https://github.com/login/oauth/authorize?' + urllib.parse.urlencode({
            'client_id': self.client_id,
            'scope': 'read:user',
            'state': state,
        })

    def _json(self, request):
        try:
            response = self.opener(request, timeout=self.timeout)
            data = json.loads(response.read())
        except (OSError, ValueError, urllib.error.HTTPError) as error:
            raise OAuthError('OAuth provider request failed') from error
        if not isinstance(data, dict):
            raise OAuthError('OAuth provider returned an invalid response')
        return data

    def exchange(self, code, state):
        if not code or not state:
            raise OAuthError('Missing OAuth code or state')
        body = urllib.parse.urlencode({
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': code,
        }).encode()
        request = urllib.request.Request(
            'https://github.com/login/oauth/access_token', body,
            {'Accept': 'application/json'},
        )
        result = self._json(request)
        token = result.get('access_token')
        scopes = {item.strip() for item in str(result.get('scope', '')).split(',') if item.strip()}
        if result.get('error') or not isinstance(token, str) or not token:
            raise OAuthError(result.get('error_description') or result.get('error') or 'OAuth provider did not issue an access token')
        if scopes and 'read:user' not in scopes:
            raise OAuthError('OAuth provider did not grant the required scope')
        request = urllib.request.Request(
            'https://api.github.com/user',
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'},
        )
        try:
            user = self._json(request)
        finally:
            token = None
        try:
            subject = str(user['id'])
            login = str(user['login'])
            created = date.fromisoformat(user['created_at'][:10])
        except (KeyError, TypeError, ValueError) as error:
            raise OAuthError('OAuth profile was incomplete') from error
        if not subject or not login:
            raise OAuthError('OAuth profile was incomplete')
        return Identity(subject, login, created)


def subject_hash(secret, subject):
    return hmac.new(secret.encode(), ('github:' + subject).encode(), hashlib.sha256).hexdigest()


def state_digest(secret, state):
    return hmac.new(secret.encode(), ('oauth-state:' + state).encode(), hashlib.sha256).hexdigest()


def _sig(secret, data):
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def pack(secret, payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    return raw + '.' + _sig(secret, raw)


def unpack(secret, value, now=None):
    try:
        raw, signature = value.rsplit('.', 1)
        if not hmac.compare_digest(signature, _sig(secret, raw)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if payload.get('exp', 0) < (now or datetime.now(timezone.utc).timestamp()):
            return None
        return payload
    except Exception:
        return None


def session(secret, uid=0, now=None, login=None, nonce=None, oauth_state=None):
    timestamp = (now or datetime.now(timezone.utc)).timestamp()
    payload = {'uid': uid, 'exp': timestamp + 86400, 'csrf': secrets.token_urlsafe(20)}
    if login:
        payload['login'] = login
    if nonce:
        payload['sid'] = nonce
    if oauth_state:
        payload['oauth_state'] = oauth_state
    return pack(secret, payload)


def cookie(name, value, secure=False, max_age=None):
    result = f'{name}={value}; Path=/; HttpOnly; SameSite=Lax'
    if secure:
        result += '; Secure'
    if max_age is not None:
        result += f'; Max-Age={max_age}'
    return result
