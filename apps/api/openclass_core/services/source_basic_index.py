"""Immutable parsed-source objects and indexes; neither grants nor read receipts."""
from __future__ import annotations
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
import sys

def _immutable(*args, **kwargs):
    raise TypeError('Parsed source cache objects are immutable.')

class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self

class FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self

def _freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList((_freeze(item) for item in value))
    return value

class BasicPositionIndex:

    def __init__(self, units):
        groups = defaultdict(list)
        self.destinations = defaultdict(list)
        for unit in units:
            location = unit['locator']
            key = (location['kind'], location.get('container', ''))
            start = location.get('start')
            if type(start) is int:
                groups[key].append((start, unit['id']))
            self.destinations[*key, start].append(unit['id'])
        self.groups = {}
        for key, pairs in groups.items():
            pairs.sort()
            self.groups[key] = (tuple((p[0] for p in pairs)), tuple((p[1] for p in pairs)))
        self.destinations = dict(self.destinations)

    def candidates(self, data, requested):
        units = data['units']
        bounds = (requested or {}).get('metadata', {})
        low, high = (bounds.get('basic_unit_start', 1), bounds.get('basic_unit_end', len(units)))
        if 'basic_unit_start' in bounds or 'basic_unit_end' in bounds:
            if type(low) is not int or type(high) is not int or (not 1 <= low <= high <= len(units)):
                return []
            return units[low - 1:high]
        if not requested or not all((type(requested.get(key)) is int for key in ('start', 'end'))):
            return units
        start, end = (requested['start'], requested['end'])
        kind, container = (requested.get('kind'), requested.get('container', ''))
        if kind in {'docx_paragraphs', 'epub_spine'} and requested.get('index_base', bounds.get('index_base', 1)) == 0:
            start += 1
            end += 1
        identifiers = []
        for (group_kind, group_container), (positions, ids) in self.groups.items():
            if group_kind != kind or (container and kind != 'epub_spine' and (container != group_container)):
                continue
            identifiers.extend(ids[bisect_left(positions, start):bisect_right(positions, end)])
        return [units[index - 1] for index in sorted(identifiers)]

class IndexedBasicContent(FrozenDict):

    def __init__(self, data):
        dict.__init__(self, {key: _freeze(value) for key, value in data.items()})
        self.position_index = BasicPositionIndex(self['units'])

def position_index(data):
    return data.position_index if isinstance(data, IndexedBasicContent) else BasicPositionIndex(data['units'])

def retained_size(value):
    """Charge cache objects and their position indexes against the existing byte budget."""
    seen = set()

    def size(item):
        if id(item) in seen:
            return 0
        seen.add(id(item))
        total = sys.getsizeof(item)
        if isinstance(item, dict):
            total += sum((size(key) + size(child) for key, child in item.items()))
        elif isinstance(item, (list, tuple)):
            total += sum((size(child) for child in item))
        if isinstance(item, IndexedBasicContent):
            total += size(item.position_index.groups) + size(item.position_index.destinations)
        return total
    return size(value)

@dataclass(frozen=True)
class BasicCacheEntry:
    data: IndexedBasicContent
    size: int

def indexed_content(data):
    return data if isinstance(data, IndexedBasicContent) else IndexedBasicContent(data)
