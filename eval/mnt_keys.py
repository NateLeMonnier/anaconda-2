"""The exact keys `rtl_matcher` files an MNT row under.

`canonicalize_place` is imported from the matcher rather than copied here. A
holdout built against a copy would keep decontaminating correctly right up
until someone changed the matcher's canonicalization, and then it would stop
silently — the eval would read as an algorithm result while the dictionary was
answering underneath it.
"""
import os
import sys

_ANACONDA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ANACONDA not in sys.path:
    sys.path.insert(0, _ANACONDA)

from rtl_matcher import canonicalize_place  # noqa: E402

__all__ = ['canonicalize_place', 'index_keys']


def index_keys(raw):
    """Every key `_load_mnt` would reach `raw` through.

    Three of them, and a holdout that removes only the first leaves two live
    paths back to the memorized answer:

      - `raw.lower()` into `mnt_by_raw`
      - the same with hyphens spaced, also into `mnt_by_raw`
      - `canonicalize_place(raw)` into `fs_by_raw`, the full-string fast path,
        built only for strings carrying a comma or semicolon

    `mnt_by_value` is deliberately not included. It is an authority-name index
    and the name is in the PA export too, so removing it would take away
    legitimate name resolution rather than a memorized mapping.
    """
    text = (raw or '').strip()
    if not text:
        return set()
    lower = text.lower()
    keys = {lower, lower.replace('-', ' ')}
    if ',' in text or ';' in text:
        keys.add(canonicalize_place(text))
    return keys
