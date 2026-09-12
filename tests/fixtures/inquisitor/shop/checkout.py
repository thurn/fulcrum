"""Checkout's original input boundary."""


def quote(raw):
    quantity = int(raw["quantity"])
    cents = int(raw["unit_cents"])
    if quantity <= 0 or cents < 0:
        raise ValueError("invalid order")
    return {"total_cents": quantity * cents}
