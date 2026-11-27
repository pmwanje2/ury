"""Identify the exact invoice being saved by an authorized outlet transfer."""
from contextlib import contextmanager
from contextvars import ContextVar

_transferring = ContextVar("outlet_transfer_invoice", default=None)


@contextmanager
def outlet_transfer_save(invoice):
    token = _transferring.set(invoice)
    try:
        yield
    finally:
        _transferring.reset(token)


def is_outlet_transfer(invoice):
    return _transferring.get() is invoice
