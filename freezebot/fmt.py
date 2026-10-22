"""Message formatting in the bounty OP's style."""
from decimal import Decimal, ROUND_HALF_UP

TEMPLATE = "{amount} ${symbol} should have gone through #Monero $XMR. Learn more at getmonero.org"


def format_amount(amount: Decimal) -> str:
    q = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{q:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def format_message(event) -> str:
    text = TEMPLATE.format(amount=format_amount(event.amount), symbol=event.symbol)
    return f"{text}\n{event.explorer_url}"
