"""Prompts for the eval labeler.

The model does all the geography. It never sees rtl_matcher output, the MNT,
or a retrieved candidate list at identify time — only the raw string.
"""
import json
import re

SYSTEM = (
    "You label place strings extracted from US newspaper articles, 1800s-1900s, "
    "for a genealogical place authority. You are producing ground truth used to "
    "grade an automated matcher, so accuracy matters more than coverage: when "
    "you do not know, say so rather than guessing."
)

_RULES = """For each numbered place string, return two independent answers.

string_only: the most specific real jurisdiction you can identify using ONLY
places actually named in the input. If the leftmost item is a building, church,
hospital, street, or other feature rather than a jurisdiction, climb to the
jurisdiction named in the string. "Bethel Lutheran church, Chicago" gives
Chicago. "Beverly Hilton Hotel" alone gives empty, because no jurisdiction is
named.

world: the same question, but you may use what you know. "Beverly Hilton Hotel"
gives Beverly Hills. When world and string_only agree, repeat the answer.

For each, give the leaf (the most specific place name, spelling corrected to
its modern canonical form, so "Sanpat" becomes "San Patricio") and the full
jurisdiction chain from that leaf up to the country, comma separated. Counties
are named without the word "County". Use "USA" for the United States.

Return empty strings for both fields when you cannot identify a place.

Respond with a JSON array and nothing else. One object per input, in order:
{"n": 1, "leaf_string_only": "", "chain_string_only": "",
 "leaf_world": "", "chain_world": ""}"""

_KEYS = ('leaf_string_only', 'chain_string_only', 'leaf_world', 'chain_world')

_HOUSE_NUMBER = re.compile(r'^\s*\d+[A-Za-z]?\s+')
_STREET_SUFFIX = re.compile(
    r'\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|'
    r'ct|court|pl|place|ter|terrace|hwy|highway|cir|circle|pkwy|parkway|way)'
    r'\.?\s*$', re.I)


def redact_for_transport(place):
    """Strip a leading house number from the leaf when the leaf is a street.

    No specific residential address reaches a third party. 29.3% of the tail
    band is address-shaped. This costs nothing in accuracy: the street is never
    in PA, so the label is the containing jurisdiction either way.

    Gated on a street suffix so real numeric place names survive — PA holds 12,
    among them "100 Mile House" and "16th Street Baptist Church". Only the leaf
    is touched; parent levels are never addresses.
    """
    if not place:
        return place
    parts = place.split(',')
    leaf = parts[0]
    if _HOUSE_NUMBER.match(leaf) and _STREET_SUFFIX.search(leaf):
        parts[0] = _HOUSE_NUMBER.sub('', leaf)
    return ','.join(parts)


def build_identify_prompt(places):
    listing = '\n'.join(f'{i}. {redact_for_transport(p["place"])}'
                        for i, p in enumerate(places, 1))
    return f'{_RULES}\n\nPlace strings:\n{listing}'


def _extract_json(text):
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    body = fenced.group(1) if fenced else text
    start, end = body.find('['), body.rfind(']')
    if start == -1 or end == -1:
        raise ValueError(f'no JSON array in response: {text[:200]!r}')
    return body[start:end + 1]


def parse_identify_response(text, expected):
    """Parse into exactly `expected` entries, padding short responses."""
    parsed = json.loads(_extract_json(text))
    if not isinstance(parsed, list):
        raise ValueError('response was not a JSON array')
    by_n = {}
    for i, item in enumerate(parsed, 1):
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get('n', i))
        except (TypeError, ValueError):
            n = i
        by_n[n] = {k: str(item.get(k) or '').strip() for k in _KEYS}
    return [by_n.get(i, {k: '' for k in _KEYS}) for i in range(1, expected + 1)]


def build_disambiguate_prompt(place, candidates):
    """Caller prepends SYSTEM, so it is deliberately absent here."""
    listing = '\n'.join(f'{i}. {c}' for i, c in enumerate(candidates, 1))
    return (f'Place string: {redact_for_transport(place)}\n\n'
            f'Place Authority holds these records under that name:\n{listing}\n\n'
            'Which one does the place string refer to? Reply with the number '
            'alone, or NONE if it is none of them or you cannot tell.')


def parse_disambiguate_response(text, n):
    """Return a zero-based index, or None for NONE / out of range / unparseable."""
    m = re.search(r'\d+', text or '')
    if not m:
        return None
    picked = int(m.group())
    return picked - 1 if 1 <= picked <= n else None
