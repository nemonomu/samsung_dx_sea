"""Collect Amazon's displayed savings percentage and normalize it for the DB."""

import re


_PERCENTAGE = re.compile(r'[-\u2212]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%')
_SAVINGS_XPATH = (
    "//*[@id='corePriceDisplay_desktop_feature_div' or @id='corePrice_feature_div']"
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' savingsPercentage ')]"
)


def normalize_savings(value):
    """Convert '-21%' to '21%'; missing or non-percentage values become NULL."""
    if not isinstance(value, str):
        return None
    match = _PERCENTAGE.fullmatch(value.strip())
    if match is None or float(match[1]) > 100:
        return None
    return match[1] + '%'


def extract_page_savings(tree):
    """Read the main PDP price badge, preserving its displayed minus sign.

    Amazon marks this visual badge aria-hidden for screen readers; that alone
    does not make it invisible. Ignore explicitly hidden DOM and other offers.
    """
    if tree is None:
        return None
    values = []
    for node in tree.xpath(_SAVINGS_XPATH):
        hidden = False
        for ancestor in (node, *node.iterancestors()):
            classes = (ancestor.get('class') or '').split()
            style = re.sub(r'\s+', '', ancestor.get('style') or '').lower()
            if (ancestor.get('hidden') is not None or 'aok-hidden' in classes
                    or 'a-offscreen' in classes or 'display:none' in style
                    or 'visibility:hidden' in style):
                hidden = True
                break
        if hidden:
            continue
        value = node.text_content().strip()
        if normalize_savings(value) is not None and value not in values:
            values.append(value)
    # Conflicting badges cannot identify a single displayed offer reliably.
    return values[0] if len(values) == 1 else None


def build_amazon_extracted_data(product, fields):
    """Build INSERT/UPDATE values from the collected badge, never price math."""
    data = {field: product.get(field) for field in fields}
    if 'savings' in data:
        data['savings'] = normalize_savings(data['savings'])
    return data
