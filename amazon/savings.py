"""Derive Amazon savings from the price strings being saved to the DB.

Only a complete, non-negative US-style amount is accepted. Availability text,
mixed price messages, ranges and missing prices produce None (SQL NULL).
The crawler's original price strings are never changed.
"""

from decimal import Decimal, localcontext
import re


_PRICE = re.compile(
    r'(?:\$\s*)?(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)'
    r'(?:\.[0-9]{1,2})?'
)


def _parse_price(value):
    """Parse a whole crawler price string, never a number inside a message."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _PRICE.fullmatch(value) is None:
        return None
    return Decimal(value.replace('$', '').replace(',', '').strip())


def calculate_savings(original_sku_price, final_sku_price):
    """Return '$1,234.56', '$0.00', or None for invalid/inverted prices."""
    original = _parse_price(original_sku_price)
    final = _parse_price(final_sku_price)
    if original is None or final is None or original < final:
        return None
    # Keep cents exact even for amounts longer than Decimal's default precision.
    with localcontext() as context:
        context.prec = max(len(original.as_tuple().digits),
                           len(final.as_tuple().digits), 28) + 2
        return f'${original - final:,.2f}'


def build_amazon_extracted_data(product, fields):
    """Build INSERT/UPDATE values, replacing stale savings from the input row."""
    data = {field: product.get(field) for field in fields}
    data['savings'] = calculate_savings(
        data.get('original_sku_price'), data.get('final_sku_price')
    )
    return data
