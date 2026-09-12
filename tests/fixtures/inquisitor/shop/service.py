"""Public adapters for the synthetic shop."""

from checkout import quote
from invoices import render


def preview(raw):
    return quote(raw), render(raw)
