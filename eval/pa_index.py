"""Leaf-first PA index for the eval labeler.

Deliberately not RTL-shaped. Traversal starts at the leaf a model proposed
and moves up, where rtl_matcher starts at the rightmost term and moves left.
Selection among same-named candidates is done by comparing the model's own
proposed chain against PA's FullChainName, never by chain-connection scoring,
evidence rank, or population. The MNT is never read.
"""
import csv
import re
import sys
import unicodedata
from collections import namedtuple

csv.field_size_limit(sys.maxsize)

PARow = namedtuple(
    'PARow',
    'level level_name uuid term full_chain parent_id population replacement_uuid')

Resolution = namedtuple('Resolution', 'uuid status candidates')

_PAREN = re.compile(r'\([^)]*\)')
_APOSTROPHE = str.maketrans('', '', "'’")
_QUALIFIER = re.compile(
    r'\b(county|parish|borough|township|twp|municipality|oblast|krai|raion)\b')


def normalize_term(s):
    """Casefold, strip accents and parentheticals, drop punctuation.

    Apostrophes are deleted rather than spaced, so "St. Mary's" gives
    "st marys" and not "st mary s". Parentheticals go because PA marks
    superseded places inline: Term "Prussia", FullChainName leaf
    "Prussia (historical)".

    Jurisdiction qualifiers are NOT stripped here — this keys the index, and
    PA's Term column already carries the bare form ("Pike") while
    FullChainName carries the qualified one ("Pike County"). Stripping here
    would collide counties with same-named cities for no gain.
    """
    if not s:
        return ''
    decomposed = unicodedata.normalize('NFKD', _PAREN.sub(' ', s))
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.lower().translate(_APOSTROPHE)
    kept = ''.join(c if c.isalnum() else ' ' for c in stripped)
    return ' '.join(kept.split())


def normalize_chain(s):
    """Normalize each comma-separated part, rejoin with ', '."""
    if not s:
        return ''
    parts = [normalize_term(p) for p in s.split(',')]
    return ', '.join(p for p in parts if p)


def _match_key(s):
    """normalize_term plus jurisdiction-qualifier stripping.

    For comparing a model-proposed chain against FullChainName only, never
    for the index. PA disagrees with itself on qualifiers — Term "Pike"
    against chain leaf "Pike County", Term "Amur" against "Amur Oblast" —
    for 12.7% of rows. Stripping both sides of the comparison takes that
    to 0.30%, the residual being rarer foreign administrative suffixes,
    which fall through to model disambiguation rather than mismatching.
    """
    return ' '.join(_QUALIFIER.sub(' ', normalize_term(s)).split())


def _chain_tokens(chain):
    """Set of match keys for a chain's parts, leaf included."""
    return {k for k in (_match_key(p) for p in (chain or '').split(',')) if k}


class PAIndex:
    """Exact index on normalized Term. The only surface shared with the matcher."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.by_term = {}
        for r in self.rows:
            self.by_term.setdefault(normalize_term(r.term), []).append(r)

    @classmethod
    def from_tsv(cls, path):
        rows = []
        with open(path, encoding='utf-8', newline='') as f:
            for rec in csv.DictReader(f, delimiter='\t'):
                rows.append(PARow(
                    level=(rec.get('Level') or '').strip(),
                    level_name=(rec.get('LevelName') or '').strip(),
                    uuid=(rec.get('ID') or '').strip(),
                    term=(rec.get('Term') or '').strip(),
                    full_chain=(rec.get('FullChainName') or '').strip(),
                    parent_id=(rec.get('ParentID') or '').strip(),
                    population=(rec.get('Population') or '').strip(),
                    replacement_uuid=(rec.get('Replacement_UUID') or '').strip(),
                ))
        return cls(rows)

    def lookup_term(self, term):
        return self.by_term.get(normalize_term(term), [])

    def resolve(self, leaf, proposed_chain):
        """Resolve a model-proposed leaf to a PA UUID.

        Returns Resolution. A status of 'needs_disambiguation' means the
        caller must present `candidates` back to the model for a pick;
        'replaced' and 'absent' mean the caller must not guess.
        """
        candidates = self.lookup_term(leaf)
        if not candidates:
            return Resolution(None, 'absent', [])

        replaced = [c for c in candidates
                    if c.replacement_uuid and c.replacement_uuid != c.uuid]
        if replaced and len(candidates) == len(replaced):
            return Resolution(None, 'replaced', candidates)

        if len(candidates) == 1:
            return Resolution(candidates[0].uuid, 'unique', candidates)

        proposed = _chain_tokens(proposed_chain) - {_match_key(leaf)}
        if not proposed:
            return Resolution(None, 'needs_disambiguation', candidates)

        scored = []
        for c in candidates:
            overlap = len(proposed & (_chain_tokens(c.full_chain)
                                      - {_match_key(c.term)}))
            scored.append((overlap, c))
        best = max(s for s, _ in scored)
        winners = [c for s, c in scored if s == best]
        if best > 0 and len(winners) == 1:
            return Resolution(winners[0].uuid, 'chain_matched', candidates)
        return Resolution(None, 'needs_disambiguation', candidates)
