"""Invoice export independently repeats the same domain rules."""


def render(raw):
    quantity = int(raw["quantity"])
    cents = int(raw["unit_cents"])
    if quantity <= 0 or cents < 0:
        raise ValueError("invalid order")
    return f"Total: {quantity * cents} cents"
