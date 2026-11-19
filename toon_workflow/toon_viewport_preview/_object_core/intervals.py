# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure interval algebra and a divide-and-conquer affine lower envelope.

Inputs are frozen to finite Python float64 values. Fraction fallbacks describe
those values exactly; they do not restore precision lost in upstream geometry.
No positive interval is removed by a length tolerance. An unrepresentable output
piece or gap, an ambiguous cross-domain tie, or a budget excess is unresolved.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from numbers import Integral, Real
import sys

from .contract import InputError


class UnresolvedVisibility(InputError):
    """The requested finite interval result cannot be returned faithfully."""


def _budget(value):
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise InputError('max_pieces must be a positive integer')
    return int(value)


def _float(value, label):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise InputError(label + ' must be a finite real number')
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise InputError(label + ' cannot be represented as finite float64') from error
    if not math.isfinite(result):
        raise InputError(label + ' must be finite')
    return result


def _interval(value, label):
    try:
        lo, hi = value
    except (TypeError, ValueError) as error:
        raise InputError(label + ' must contain exactly lo and hi') from error
    lo, hi = _float(lo, label + '.lo'), _float(hi, label + '.hi')
    if lo > hi:
        raise InputError(label + ' has reversed bounds')
    return lo, hi


def _iterable(value, label):
    try:
        return iter(value)
    except TypeError as error:
        raise InputError(label + ' must be iterable') from error


def union_intervals(intervals, *, max_pieces=100000):
    """Return sorted, disjoint positive spans; touching spans are united.

    Bounds may be anywhere on the finite real line. Reversed/nonfinite bounds
    are invalid, while zero-measure intervals contribute nothing.
    """
    limit = _budget(max_pieces)
    spans = [_interval(value, 'interval') for value in _iterable(intervals, 'intervals')]
    spans.sort()
    result = []
    for lo, hi in spans:
        if lo == hi:
            continue
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], hi))
        else:
            result.append((lo, hi))
            if len(result) > limit:
                raise UnresolvedVisibility('Interval union exceeds max_pieces')
    return result


def subtract_intervals(base, blocked, *, max_pieces=100000):
    """Subtract the union of blocked spans from one base interval.

    Endpoint inclusion is immaterial here: only positive-measure spans are
    returned. Blocked intervals are validated even when the base is empty.
    """
    limit = _budget(max_pieces)
    lo, hi = _interval(base, 'base')
    blocks = union_intervals(blocked, max_pieces=limit)
    result = []
    cursor = lo
    for start, end in blocks:
        if end <= cursor:
            continue
        if start >= hi:
            break
        if start > cursor:
            result.append((cursor, min(start, hi)))
            if len(result) > limit:
                raise UnresolvedVisibility('Interval subtraction exceeds max_pieces')
        cursor = min(hi, max(cursor, end))
        if cursor >= hi:
            break
    if cursor < hi:
        result.append((cursor, hi))
        if len(result) > limit:
            raise UnresolvedVisibility('Interval subtraction exceeds max_pieces')
    return result


@dataclass(frozen=True)
class _Source:
    index: int
    row: dict
    a: float
    b: float
    aq: Fraction
    bq: Fraction
    ambiguous: bool = False


@dataclass(frozen=True)
class _Piece:
    lo: Fraction
    hi: Fraction
    source: _Source


def _compare(left, right, parameter):
    """Compare frozen affine depths; uncertified float signs fall back exactly."""
    u = float(parameter)
    lt, rt = left.b * u, right.b * u
    lv, rv = left.a + lt, right.a + rt
    difference = lv - rv
    # This deliberately generous arithmetic bound includes evaluation rounding,
    # parameter conversion rounding and subnormal absolute rounding. Overflow
    # makes the fast path uncertified and therefore selects the exact fallback.
    scale = abs(left.a) + abs(right.a) + abs(lt) + abs(rt)
    error = (64 * sys.float_info.epsilon * scale
             + (abs(left.b) + abs(right.b)) * math.ulp(u)
             + 32 * math.ulp(0.0))
    if math.isfinite(difference) and math.isfinite(error) and abs(difference) > error:
        return -1 if difference < 0 else 1
    exact = left.aq - right.aq + (left.bq - right.bq) * parameter
    return (exact > 0) - (exact < 0)


def _same_domain(left, right):
    try:
        return bool(left.row['domain'] == right.row['domain'])
    except (TypeError, ValueError) as error:
        raise InputError('domain identities must support scalar equality') from error


def _append(result, lo, hi, source, limit):
    if lo == hi:
        return
    if lo > hi:
        raise UnresolvedVisibility('Internal interval order is unresolved')
    if (result and result[-1].hi == lo and result[-1].source.index == source.index
            and result[-1].source.ambiguous == source.ambiguous):
        result[-1] = _Piece(result[-1].lo, hi, source)
    else:
        result.append(_Piece(lo, hi, source))
        if len(result) > limit:
            raise UnresolvedVisibility('Lower envelope exceeds max_pieces')


def _overlap(result, lo, hi, left, right, limit):
    if left.a == right.a and left.b == right.b:
        winner = min((left, right), key=lambda source: (source.row['id'], source.index))
        ambiguous = left.ambiguous or right.ambiguous or not _same_domain(left, right)
        if ambiguous:
            winner = _Source(winner.index, winner.row, winner.a, winner.b, winner.aq, winner.bq, True)
        _append(result, lo, hi, winner, limit)
        return
    low_sign, high_sign = _compare(left, right, lo), _compare(left, right, hi)
    if low_sign * high_sign < 0:
        denominator = left.bq - right.bq
        if not denominator:
            raise UnresolvedVisibility('Affine crossing has no finite exact root')
        root = (right.aq - left.aq) / denominator
        if not lo < root < hi:
            raise UnresolvedVisibility('Affine crossing root is outside its exact overlap')
        first, second = (left, right) if low_sign < 0 else (right, left)
        _append(result, lo, root, first, limit)
        _append(result, root, hi, second, limit)
    else:
        sign = low_sign or high_sign
        if not sign:
            # Distinct affine coefficients cannot tie at two distinct points.
            raise UnresolvedVisibility('Affine comparison is unresolved')
        _append(result, lo, hi, left if sign < 0 else right, limit)


def _merge(left, right, limit):
    left, right = left.copy(), right.copy()
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        p, q = left[i], right[j]
        if p.hi <= q.lo:
            _append(result, p.lo, p.hi, p.source, limit)
            i += 1
            continue
        if q.hi <= p.lo:
            _append(result, q.lo, q.hi, q.source, limit)
            j += 1
            continue
        lo, hi = max(p.lo, q.lo), min(p.hi, q.hi)
        if p.lo < lo:
            _append(result, p.lo, lo, p.source, limit)
        if q.lo < lo:
            _append(result, q.lo, lo, q.source, limit)
        _overlap(result, lo, hi, p.source, q.source, limit)
        if p.hi == hi:
            i += 1
        else:
            p = _Piece(hi, p.hi, p.source)
            left[i] = p
        if q.hi == hi:
            j += 1
        else:
            q = _Piece(hi, q.hi, q.source)
            right[j] = q
    for p in left[i:]:
        _append(result, p.lo, p.hi, p.source, limit)
    for q in right[j:]:
        _append(result, q.lo, q.hi, q.source, limit)
    return result


def _divide(pieces, limit):
    if len(pieces) <= 1:
        return pieces
    middle = len(pieces) // 2
    return _merge(_divide(pieces[:middle], limit), _divide(pieces[middle:], limit), limit)


def _exact_envelope(segments, limit):
    pieces = []
    for index, segment in enumerate(_iterable(segments, 'segments')):
        if not isinstance(segment, dict):
            raise InputError('Each affine segment must be a dictionary')
        missing = {'lo', 'hi', 'a', 'b', 'id', 'domain'} - segment.keys()
        if missing:
            raise InputError('Affine segment is missing: ' + ', '.join(sorted(missing)))
        row = dict(segment)
        lo, hi = _interval((row['lo'], row['hi']), 'segment')
        a, b = _float(row['a'], 'segment.a'), _float(row['b'], 'segment.b')
        if isinstance(row['id'], bool) or not isinstance(row['id'], Integral):
            raise InputError('Affine segment id must be an integer')
        row.update(lo=lo, hi=hi, a=a, b=b, id=int(row['id']))
        if lo == hi:
            continue
        source = _Source(index, row, a, b, Fraction.from_float(a), Fraction.from_float(b))
        pieces.append(_Piece(Fraction.from_float(lo), Fraction.from_float(hi), source))
    return _divide(pieces, limit)


def lower_envelope(segments, *, max_pieces=100000):
    """Return the closest affine depth on each covered positive span.

    Each input dictionary requires finite lo/hi/a/b, integer id, and domain.
    Depth is a+b*u. Additional metadata is shallow-copied from the selected
    original segment. Same-depth same-domain spans select the smallest id
    (then stable input order for duplicate ids). Cross-domain ties of positive
    length on the final nearest envelope raise UnresolvedVisibility; buried
    equal-depth spans and isolated crossing points do not.

    Exact Fraction boundaries survive recursive merges. The public result uses
    nearest float64 boundaries; distinct exact positive spans/gaps may never
    collapse during conversion. Such cases raise rather than losing a feature.
    max_pieces bounds each intermediate and final envelope, never filters it.
    """
    limit = _budget(max_pieces)
    envelope = _exact_envelope(segments, limit)
    result = []
    previous = None
    for piece in envelope:
        if piece.source.ambiguous:
            raise UnresolvedVisibility('Equal-depth nearest positive span has different domains')
        lo, hi = float(piece.lo), float(piece.hi)
        if not math.isfinite(lo) or not math.isfinite(hi) or not lo < hi:
            raise UnresolvedVisibility('Positive exact envelope piece is not representable in float64')
        if not piece.source.row['lo'] <= lo < hi <= piece.source.row['hi']:
            raise UnresolvedVisibility('Rounded envelope bounds leave the source interval')
        if previous is not None:
            old_piece, old_hi = previous
            if old_piece.hi > piece.lo or old_hi > lo:
                raise UnresolvedVisibility('Envelope pieces overlap or changed order')
            if old_piece.hi < piece.lo and old_hi == lo:
                raise UnresolvedVisibility('Positive exact envelope gap collapsed in float64')
        row = dict(piece.source.row)
        row.update(lo=lo, hi=hi)
        result.append(row)
        previous = piece, hi
    return result


def domain_envelope(segments, *, max_pieces=100000):
    """Return only nearest-domain spans, coalescing before float conversion.

    Input validation and exact nearest-depth semantics match lower_envelope.
    Adjacent pieces with the same domain are joined only when their exact
    Fraction boundaries touch. Internal triangle-identity changes therefore
    need not be representable as public float boundaries. Positive gaps and
    different domains are never joined; unresolved nearest-domain ties fail.

    Rows contain lo, hi, domain, witness_ids (sorted representative winning
    integer IDs), and depth_model='nearest_piecewise_affine'. They deliberately
    contain no single a/b/id: one domain span can have several depth functions.
    The budget still bounds intermediate identity pieces as well as output.
    """
    limit = _budget(max_pieces)
    envelope = _exact_envelope(segments, limit)
    groups = []
    for piece in envelope:
        if piece.source.ambiguous:
            raise UnresolvedVisibility('Equal-depth nearest positive span has different domains')
        if (groups and groups[-1]['hi'] == piece.lo
                and _same_domain(groups[-1]['source'], piece.source)):
            groups[-1]['hi'] = piece.hi
            groups[-1]['witness_ids'].add(piece.source.row['id'])
        else:
            groups.append({'lo': piece.lo, 'hi': piece.hi, 'source': piece.source,
                           'witness_ids': {piece.source.row['id']}})
            if len(groups) > limit:
                raise UnresolvedVisibility('Domain envelope exceeds max_pieces')
    result = []
    previous = None
    for group in groups:
        lo, hi = float(group['lo']), float(group['hi'])
        if not math.isfinite(lo) or not math.isfinite(hi) or not lo < hi:
            raise UnresolvedVisibility('Positive exact domain piece is not representable in float64')
        if previous is not None:
            exact_hi, old_hi = previous
            if exact_hi > group['lo'] or old_hi > lo:
                raise UnresolvedVisibility('Domain pieces overlap or changed order')
            if exact_hi < group['lo'] and old_hi == lo:
                raise UnresolvedVisibility('Positive exact domain gap collapsed in float64')
        result.append({'lo': lo, 'hi': hi, 'domain': group['source'].row['domain'],
                       'witness_ids': tuple(sorted(group['witness_ids'])),
                       'depth_model': 'nearest_piecewise_affine'})
        previous = group['hi'], hi
    return result
