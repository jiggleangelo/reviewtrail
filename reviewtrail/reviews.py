import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

CATEGORIES = {'coding', 'extraction', 'rag', 'agents'}
HARNESSES = {'api-direct', 'openrouter', 'ide-plugin', 'cli-agent'}
ALIASES = {'latest', 'default', 'stable', 'auto', 'preview'}
VERSION = re.compile(r'^[A-Za-z0-9._:/@+\-]{3,80}$')
ASCII_INT = re.compile(r'^[0-9]+$')
EMAIL_PART = re.compile(r'[^\s@]+')
WORD = re.compile(r'\w')
PREFIX_SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[A-Z0-9]{12,})')
CONTEXT_SECRET = re.compile(r'(?i)\b(?:api[_ -]?key|secret|token|password|credential)\s*[:=]\s*[A-Za-z0-9+/=_-]{16,}')
BARE_TOKEN = re.compile(r'(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/]{32,}={0,2}(?![A-Za-z0-9_+/=-])')
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PHONE_CHARS = frozenset('0123456789 +-.()')


@dataclass
class ReviewInput:
    model_slug: str
    model_version: str
    task_category: str
    task_description: str
    stack: str
    harness: str
    outcome: int
    retries: int
    cost_usd_cents: int | None
    used_on: str
    iso_week: str
    proof_sha256: str | None
    show_handle: bool
    content_hash: str


def specific(value):
    return bool(any(character.isdigit() for character in value)) and value.lower() not in ALIASES


def iso_week(value):
    year, week, _ = value.isocalendar()
    return f'{year}-W{week:02d}'


def content_hash(description, stack, version):
    normalized = ' '.join((description + ' ' + stack + ' ' + version.lower()).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def proof_hash(value, key):
    raw = key.encode() if isinstance(key, str) else key
    return hmac.new(raw, b'review-proof\0' + value.encode(), hashlib.sha256).hexdigest()


def _looks_like_email(value):
    previous_end = -1
    previous_has_word = False
    for part in EMAIL_PART.finditer(value):
        text = part.group()
        if previous_has_word and part.start() == previous_end + 1 and value[previous_end] == '@':
            dot = text.find('.', 1)
            if dot != -1 and WORD.search(text, dot + 1):
                return True
        previous_end = part.end()
        previous_has_word = WORD.search(text) is not None
    return False


def _looks_like_phone(value):
    start = None
    for index in range(len(value) + 1):
        character = value[index] if index < len(value) else ''
        if character and character in PHONE_CHARS:
            if start is None:
                start = index
            continue
        if start is not None:
            candidate = value[start:index].strip()
            digits = sum(character.isascii() and character.isdigit() for character in candidate)
            if digits >= 8 and not ISO_DATE.fullmatch(candidate):
                return True
        start = None
    return False


def has_sensitive(value, phone=True):
    return bool(_looks_like_email(value) or PREFIX_SECRET.search(value) or CONTEXT_SECRET.search(value) or BARE_TOKEN.search(value) or (phone and _looks_like_phone(value)))


def validate(form, today, known_slugs, proof_key='reviewtrail-test-proof-key', max_age_days=90):
    errors = {}
    get = lambda key: str(form.get(key, '')).strip()
    model = get('model_slug')
    version = get('model_version').lower()
    category = get('task_category')
    description = get('task_description')
    stack = get('stack')
    selected_harness = get('harness')
    harness_other = get('harness_other')
    harness = 'other:' + harness_other if selected_harness == 'other' else selected_harness
    proof = get('proof_line')
    if model not in known_slugs:
        errors['model_slug'] = 'Choose a known model.'
    if not VERSION.fullmatch(version):
        errors['model_version'] = 'Use 3–80 version characters.'
    if category not in CATEGORIES:
        errors['task_category'] = 'Choose a task category.'
    if not 40 <= len(description) <= 1000:
        errors['task_description'] = 'Use 40–1,000 characters.'
    if not 2 <= len(stack) <= 80:
        errors['stack'] = 'Use 2–80 characters.'
    if harness not in HARNESSES and not (harness.startswith('other:') and 0 < len(harness[6:]) <= 40):
        errors['harness'] = 'Choose a harness or provide other details.'
    if len(proof) > 300:
        errors['proof_line'] = 'Use at most 300 characters.'
    if 'model_version' not in errors and has_sensitive(version, phone=False):
        errors['model_version'] = 'Remove personal data or secrets.'
    for key, value in (('task_description', description), ('stack', stack), ('harness', harness)):
        if key not in errors and has_sensitive(value):
            errors[key] = 'Remove personal data, phone numbers, or secrets.'
    if proof and 'proof_line' not in errors and has_sensitive(proof):
        errors['proof_line'] = 'Remove personal data or secrets.'
    outcome_text = get('outcome')
    if not ASCII_INT.fullmatch(outcome_text):
        outcome = 0
        errors['outcome'] = 'Choose 1–5.'
    else:
        outcome = int(outcome_text)
        if not 1 <= outcome <= 5:
            errors['outcome'] = 'Choose 1–5.'
    retries_text = get('retries')
    if not ASCII_INT.fullmatch(retries_text):
        retries = -1
        errors['retries'] = 'Use 0–50.'
    else:
        retries = int(retries_text)
        if not 0 <= retries <= 50:
            errors['retries'] = 'Use 0–50.'
    cost = None
    if get('cost_usd'):
        try:
            amount = Decimal(get('cost_usd'))
            if not amount.is_finite() or amount < 0 or amount > 10000 or amount.as_tuple().exponent < -2:
                raise ValueError
            cost = int(amount * 100)
        except (InvalidOperation, ValueError, OverflowError):
            errors['cost_usd'] = 'Use 0–10000 with at most two decimals.'
    try:
        used = date.fromisoformat(get('used_on'))
        age = (today - used).days
        if age < 0 or age > max_age_days:
            raise ValueError
    except (TypeError, ValueError):
        used = today
        errors['used_on'] = f'Date must be within the past {max_age_days} days.'
    if errors:
        return None, errors
    return ReviewInput(model, version, category, description, stack, harness, outcome, retries, cost, used.isoformat(), iso_week(used), proof_hash(proof, proof_key) if proof else None, form.get('show_handle') == 'on', content_hash(description, stack, version)), {}


def insert(db, user_id, review, source='user'):
    from .db import now
    db.execute('''INSERT INTO reviews(user_id,model_slug,model_version,task_category,task_description,stack,harness,outcome,retries,cost_usd_cents,used_on,iso_week,proof_sha256,source,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (user_id, review.model_slug, review.model_version, review.task_category, review.task_description, review.stack, review.harness, review.outcome, review.retries, review.cost_usd_cents, review.used_on, review.iso_week, review.proof_sha256, source, review.content_hash, now()))
