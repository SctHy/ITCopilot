"""Stable opaque identity scope shared by memory, receipts and stream ownership."""
import hashlib
import json


def identity_scope(tenant: str, subject: str) -> str:
    if not tenant or not subject:
        raise ValueError('tenant and subject are required')
    # Structured encoding avoids delimiter collisions; never fall back to an unscoped key.
    encoded = json.dumps([tenant, subject], ensure_ascii=True, separators=(',', ':'))
    return 'identity:v1:' + hashlib.sha256(encoded.encode()).hexdigest()
