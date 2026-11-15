#!/usr/bin/env python3
"""Offline DPO-only vault exporter.

This module intentionally does not import reviewtrail.vault. Keep this tool and
its key outside the web application's import path and runtime credentials.
"""

import argparse
import base64
import hashlib
import hmac
import json


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


def decrypt(ciphertext, key):
    packed = base64.urlsafe_b64decode(ciphertext.encode('ascii'))
    if len(packed) < 64:
        raise ValueError('Invalid vault ciphertext')
    nonce, body = packed[:32], packed[32:]
    encrypted, supplied_tag = body[:-32], body[-32:]
    encryption_key = _derive(key, b'encryption')
    authentication_key = _derive(key, b'authentication')
    expected_tag = hmac.new(
        authentication_key,
        b'reviewtrail-contact-v2\0' + nonce + encrypted,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise ValueError('Vault ciphertext authentication failed')
    stream = _stream(encryption_key, nonce, len(encrypted))
    return bytes(a ^ b for a, b in zip(encrypted, stream)).decode('utf-8')


def export(path, key):
    live = {}
    dead = set()
    try:
        source = open(path, encoding='utf-8')
    except FileNotFoundError:
        return []
    with source:
        for line in source:
            item = json.loads(line)
            if 'tombstone' in item:
                dead.add(item['tombstone'])
                live.pop(item['tombstone'], None)
            elif item.get('token') not in dead:
                live[item['token']] = item
    return [{
        'token': item['token'],
        'kind': item['kind'],
        'value': decrypt(item['ciphertext'], key),
        'ts': item.get('ts'),
    } for item in live.values()]


def main():
    parser = argparse.ArgumentParser(description='Offline DPO vault export')
    parser.add_argument('path')
    parser.add_argument('--key', required=True, help='Offline vault recovery key')
    args = parser.parse_args()
    for item in export(args.path, args.key):
        print(json.dumps(item, separators=(',', ':')))


if __name__ == '__main__':
    main()
