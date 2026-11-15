import calendar
import hashlib
import hmac
from collections import OrderedDict
from datetime import date, datetime, timezone


def account_old_enough(account_month, today=None):
    today = today or datetime.now(timezone.utc).date()
    year, month = map(int, account_month.split('-'))
    opened = date(year, month, calendar.monthrange(year, month)[1])
    return (today - opened).days >= 90


def review_error(db, uid, r, current_time):
    since = datetime.fromtimestamp(current_time.timestamp() - 86400, timezone.utc).isoformat()
    if db.execute('SELECT COUNT(*) FROM reviews WHERE user_id=? AND created_at>=?', (uid, since)).fetchone()[0] >= 5:
        return 'At most 5 reviews per rolling 24 hours.'
    if db.execute('SELECT 1 FROM reviews WHERE user_id=? AND model_slug=? AND lower(model_version)=lower(?) AND iso_week=?', (uid, r.model_slug, r.model_version, r.iso_week)).fetchone():
        return 'One review per model version and week.'
    if db.execute('SELECT 1 FROM reviews WHERE user_id=? AND content_hash=?', (uid, r.content_hash)).fetchone():
        return 'This looks like a duplicate review.'
    return None


class IPBucket:
    def __init__(self, secret, clock=lambda: datetime.now(timezone.utc).timestamp(), capacity=30, max_keys=10000):
        self.secret = secret.encode()
        self.clock = clock
        self.capacity = capacity
        self.max_keys = max_keys
        self.b = OrderedDict()
        self.last_sweep = 0

    def allow(self, ip):
        key = hmac.new(self.secret, ip.encode(), hashlib.sha256).hexdigest()
        current = self.clock()
        if current - self.last_sweep >= 3600:
            self.b = OrderedDict((k, value) for k, value in self.b.items() if current - value[1] < 7200)
            self.last_sweep = current
        tokens, last = self.b.pop(key, (float(self.capacity), current))
        tokens = min(float(self.capacity), tokens + max(0, current - last) / 120)
        allowed = tokens >= 1
        self.b[key] = (tokens - 1 if allowed else tokens, current)
        while len(self.b) > self.max_keys:
            self.b.popitem(last=False)
        return allowed
