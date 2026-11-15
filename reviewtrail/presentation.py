"""Escaped public content and bounded, client-held OAuth handle claims."""
import base64
import hashlib
import hmac
import secrets
from pathlib import Path
from .reviews import specific
from .templates import e

ROOT = Path(__file__).resolve().parent.parent


def policy(name):
    if name not in {'methodology', 'independence', 'privacy'}:
        raise ValueError('Unknown policy')
    return '<pre>' + e((ROOT / 'content' / (name + '.md')).read_text(encoding='utf-8')) + '</pre>'


def review_list(db, slug):
    rows = db.execute(
        '''SELECT r.*,u.display_name FROM reviews r LEFT JOIN users u ON u.id=r.user_id
           WHERE r.model_slug=? AND r.status='published' ORDER BY r.used_on DESC,r.id DESC''',
        (slug,),
    ).fetchall()
    sources = {'user': 'Community review', 'founder_seed': 'Founder-written seed review',
               'consented_import': 'Consented import'}
    articles = []
    for row in rows:
        badges = []
        if specific(row['model_version']):
            badges.append('version-specific')
        if row['proof_sha256']:
            badges.append('proof attached (self-attested)')
        badge_html = ' '.join('<span class="badge">' + e(badge) + '</span>' for badge in badges)
        author = row['display_name'] or 'Pseudonymous reviewer'
        if row['source'] != 'user':
            author = sources[row['source']]
        cost = 'Not supplied' if row['cost_usd_cents'] is None else f"${row['cost_usd_cents'] / 100:.2f}"
        articles.append(
            '<article><h3>' + e(row['model_version']) + '</h3><p>' + badge_html + '</p>'
            '<p>' + e(sources[row['source']]) + ' · ' + e(author) + '</p>'
            '<dl>' + ''.join('<dt>' + e(label) + '</dt><dd>' + e(value) + '</dd>' for label, value in (
                ('Task category', row['task_category']), ('Date used', row['used_on']),
                ('Stack', row['stack']), ('Harness', row['harness']),
                ('Outcome', str(row['outcome']) + '/5'), ('Retries', row['retries']), ('Cost', cost),
            )) + '</dl><p>' + e(row['task_description']) + '</p></article>'
        )
    return '<h2>Individual reviews</h2>' + (''.join(articles) or '<p>No published reviews yet.</p>')


def _mask(key, nonce, length):
    raw = key.encode()
    return b''.join(hmac.new(raw, b'handle-claim\0' + nonce + i.to_bytes(4, 'big'), hashlib.sha256).digest()
                    for i in range((length + 31) // 32))[:length]


def seal_handle(key, login):
    """Only place this ciphertext inside an authenticated, expiring session.

    No handle candidate is kept in server memory or storage. The signed session
    authenticates the ciphertext; the separate key hides it in the browser cookie.
    This uses the same stdlib HMAC-stream approach as the existing vault, not a
    claim of protection from a compromised application process.
    """
    raw = login.encode('utf-8')
    if not 1 <= len(raw) <= 128:
        raise ValueError('Invalid OAuth handle length')
    nonce = secrets.token_bytes(32)
    ciphertext = bytes(a ^ b for a, b in zip(raw, _mask(key, nonce, len(raw))))
    return base64.urlsafe_b64encode(nonce + ciphertext).decode('ascii')


def open_handle(key, claim):
    """Caller must first validate the containing session signature and lifetime."""
    try:
        if not isinstance(claim, str) or len(claim) > 216:
            return None
        raw = base64.b64decode(claim, altchars=b'-_', validate=True)
        if not 33 <= len(raw) <= 160:
            return None
        nonce, ciphertext = raw[:32], raw[32:]
        return bytes(a ^ b for a, b in zip(ciphertext, _mask(key, nonce, len(ciphertext)))).decode('utf-8')
    except (ValueError, UnicodeError):
        return None
