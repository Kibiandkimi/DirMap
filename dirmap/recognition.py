"""Reconcile fresh automatic candidates with explicit teacher corrections."""
import copy
import math


def reconcile_controls(detected, previous):
    fresh = copy.deepcopy(detected)
    old = previous.get('controls', [])
    matches = []
    for i, item in enumerate(old):
        for j, candidate in enumerate(fresh):
            distance = math.dist(item['pixel'], candidate['pixel'])
            limit = max(5., min(item.get('radiusPx', 15), candidate.get('radiusPx', 15))*.7)
            if distance <= limit:
                matches.append((distance/limit, i, j))
    pairs, used = {}, set()
    for _, i, j in sorted(matches):
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)
    result, preserved, unmatched = [], [], []
    for i, item in enumerate(old):
        explicit = (item['id'].startswith('manual-') or item.get('state') in ('accepted', 'rejected')
                    or item.get('labelSource') == 'manual' or bool(item.get('manualFields')))
        if i in pairs:
            candidate = fresh[pairs[i]]
            if explicit:
                result.append(copy.deepcopy(item))
                preserved.append(item['id'])
            else:
                candidate['id'] = item['id']
                for key in ('required', 'serviceBaseSec', 'servicePerPersonSec'):
                    if key in item:
                        candidate[key] = item[key]
                result.append(candidate)
        elif explicit:
            result.append(copy.deepcopy(item))
            preserved.append(item['id'])
            if item.get('state') != 'rejected':
                unmatched.append(item['id'])
    occupied = {item['id'] for item in old}
    exclusions = previous.get('recognitionExclusions', [])
    for index, candidate in enumerate(fresh):
        if index in used:
            continue
        if any(math.dist(candidate['pixel'], item['pixel']) <= max(5., min(candidate['radiusPx'], item.get('radiusPx', 15))*.7)
               for item in exclusions):
            continue
        name = candidate['id']
        suffix = 1
        while name in occupied:
            name = f"{candidate['id']}-auto-{suffix}"
            suffix += 1
        candidate['id'] = name
        occupied.add(name)
        result.append(candidate)
    result.sort(key=lambda c: (c['pixel'][1], c['pixel'][0]))
    return result, {'automaticCandidates': len(detected), 'preservedTeacherControls': preserved,
                    'unmatchedTeacherControls': unmatched}
