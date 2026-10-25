"""Deterministic class-wide route improvement and polynomial special cases."""
from collections import Counter
import itertools
import time

import numpy as np

from .route_scores import RouteScores, improves


def validate_sequences(sequences, count, teams, mode, mandatory=(), ordered=()):
    if len(sequences) != teams:
        raise ValueError('已有路线的队数不匹配')
    expected = set(range(1, count+1))
    for route in sequences:
        if (len(route) < 3 or route[0] != 0 or route[-1] != count+1
                or any(not isinstance(n, (int, np.integer)) or isinstance(n, bool) for n in route)
                or len(set(route[1:-1])) != len(route[1:-1]) or not set(route[1:-1]).issubset(expected)):
            raise ValueError('已有路线包含错误端点、重复点或不存在的点')
        if not set(mandatory).issubset(route):
            raise ValueError('已有路线遗漏了每队必到点')
        if mode in ('all_visit', 'fixed_order') and set(route[1:-1]) != expected:
            raise ValueError('已有路线未覆盖每队的全部目标')
        if mode == 'fixed_order' and list(route) != list(range(count+2)):
            raise ValueError('已有路线不符合固定顺序')
    actual = Counter(n for route in sequences for n in route[1:-1])
    if mode in ('split_coverage', 'mixed_coverage'):
        required = Counter({n: teams if n in mandatory else 1 for n in expected})
        if actual != required:
            raise ValueError('已有路线不符合全班分担规则')
    elif mode == 'custom' and set(actual) != expected:
        raise ValueError('已有自定义路线没有覆盖所有目标')
    common = tuple(n for n in sequences[0] if n in mandatory)
    if any(tuple(n for n in route if n in mandatory) != common for route in sequences):
        raise ValueError('已有路线的集合顺序不一致')
    if ordered and common != tuple(ordered):
        raise ValueError('已有路线不符合指定集合顺序')


def singleton_assignment(matrix, teams, service, nodes, objective):
    """Exactly match K teams to K single visits, with a true two-stage goal."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import maximum_bipartite_matching, min_weight_full_bipartite_matching
    if len(nodes) != len(teams) or len(set(nodes)) != len(nodes):
        raise ValueError('单点分配需要与队数相同的独立目标')
    nodes = np.asarray(nodes, dtype=int)
    speed = np.array([t['speedMps'] for t in teams])[:, None]
    depart = np.array([t.get('departureSec', 0) for t in teams])[:, None]
    finish = (depart+matrix[0, nodes][None, :]/speed+np.asarray(service)[:, nodes-1]
              +matrix[nodes, -1][None, :]/speed)
    if objective == 'min_max_finish_then_sum':
        bounds = np.unique(finish)
        low, high = 0, len(bounds)-1
        while low < high:
            middle = (low+high)//2
            matching = maximum_bipartite_matching(csr_matrix(finish <= bounds[middle]), perm_type='column')
            if np.all(matching >= 0):
                high = middle
            else:
                low = middle+1
        allowed = finish <= bounds[low]
    else:
        # With common endpoints the total distance of singleton tours is
        # assignment-independent. Use the time sum as a useful tie-break.
        allowed = np.ones_like(finish, dtype=bool)
    # Sparse matching treats zero entries as absent edges. The same positive
    # offset on every chosen edge preserves the objective and permits zero
    # travel/service costs without deleting valid assignments.
    row, column = min_weight_full_bipartite_matching(csr_matrix(np.where(allowed, finish+1, 0.)))
    assigned = dict(zip(map(int, row), map(int, column)))
    return [[0, int(nodes[assigned[i]]), len(matrix)-1] for i in range(len(teams))]


def nearest_order(matrix, nodes):
    remaining, result, previous = set(nodes), [], 0
    while remaining:
        node = min(remaining, key=lambda n: (matrix[previous, n], n))
        remaining.remove(node)
        result.append(node)
        previous = node
    return tuple(result)


def initial_routes(matrix, teams, service, objective, common=(), policy='far'):
    last = len(matrix)-1
    targets = sorted((n for n in range(1, last) if n not in common), key=lambda n: (-matrix[0, n], n))
    if policy == 'near':
        targets.reverse()
    if common:
        routes = [[0, *common, last] for _ in teams]
        pending = targets
    else:
        seeds = targets[:len(teams)]
        if policy == 'spread':
            seeds = [targets[0]]
            while len(seeds) < len(teams):
                seeds.append(max((n for n in targets if n not in seeds),
                    key=lambda n: (min((matrix[s, n]+matrix[n, s])/2 for s in seeds), -n)))
        routes = (singleton_assignment(matrix, teams, service, seeds, objective) if policy in ('spread', 'matched')
                  else [[0, seed, last] for seed in seeds])
        pending = [n for n in targets if n not in seeds]
    for node in pending:
        scores = RouteScores(matrix, routes, teams, service, objective, common)
        best = None
        for team, route in enumerate(routes):
            for position in range(1, len(route)):
                a, b = route[position-1:position+1]
                delta = matrix[a, node]+matrix[node, b]-matrix[a, b]
                value = scores.insertion(team, scores.stages[team][position-1], delta, service[team][node-1])
                if best is None or value < best[0]:
                    best = (value, team, position)
        routes[best[1]].insert(best[2], node)
    return routes


def improve_routes(matrix, routes, teams, service, objective, mandatory, deadline):
    """Variable-neighbourhood descent: reversals, relocations, then swaps.

    Every accepted move improves the whole class objective, including its
    gathering waits. A swap never temporarily empties a team's route.
    """
    routes = [list(r) for r in routes]
    while time.perf_counter() < deadline:
        scores = RouteScores(matrix, routes, teams, service, objective, mandatory)
        old = scores.score()
        order = sorted(range(len(teams)), key=lambda k: (-scores.finishes[k], k))
        changed = False
        for team in order:
            route = routes[team]
            reverse = [0.]
            for a, b in zip(route, route[1:]):
                reverse.append(reverse[-1]+matrix[b, a]-matrix[a, b])
            for i in range(1, len(route)-2):
                for j in range(i+1, len(route)-1):
                    if time.perf_counter() >= deadline:
                        return routes
                    crossed = scores.stages[team][j]-scores.stages[team][i-1]
                    if crossed > 1:
                        continue
                    if crossed:
                        candidate = route[:i]+list(reversed(route[i:j+1]))+route[j+1:]
                        value = scores.replacements({team: candidate})
                    else:
                        a, first, last, b = route[i-1], route[i], route[j], route[j+1]
                        delta = matrix[a, last]+matrix[first, b]-matrix[a, first]-matrix[last, b]+reverse[j]-reverse[i]
                        value = scores.changes(delta, [(team, scores.stages[team][i-1], delta/teams[team]['speedMps'])])
                    if improves(value, old):
                        routes[team], changed = route[:i]+list(reversed(route[i:j+1]))+route[j+1:], True
                        break
                if changed:
                    break
            if changed:
                break
        if changed:
            continue
        # A node can move within its own route, including across a meeting.
        # Reversals alone cannot reach every improving insertion order.
        for a, b in itertools.product(order, repeat=2):
            if a != b and len(routes[a]) <= 3:
                continue
            for i in range(1, len(routes[a])-1):
                node = routes[a][i]
                if node in mandatory:
                    continue
                previous, following = routes[a][i-1], routes[a][i+1]
                remove = matrix[previous, following]-matrix[previous, node]-matrix[node, following]
                source_stage = scores.stages[a][i-1]
                remove_time = remove/teams[a]['speedMps']-service[a][node-1]
                for j in range(1, len(routes[b])):
                    if time.perf_counter() >= deadline:
                        return routes
                    if a == b and j in (i, i+1):
                        continue
                    previous, following = routes[b][j-1], routes[b][j]
                    insert = matrix[previous, node]+matrix[node, following]-matrix[previous, following]
                    updates = [(a, source_stage, remove_time),
                               (b, scores.stages[b][j-1], insert/teams[b]['speedMps']+service[b][node-1])]
                    if improves(scores.changes(remove+insert, updates), old):
                        node = routes[a].pop(i)
                        routes[b].insert(j-(a == b and j > i), node)
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if changed:
            continue
        for a, b in itertools.combinations(order, 2):
            for i in range(1, len(routes[a])-1):
                if routes[a][i] in mandatory:
                    continue
                for j in range(1, len(routes[b])-1):
                    if time.perf_counter() >= deadline:
                        return routes
                    if routes[b][j] in mandatory:
                        continue
                    first, second = routes[a][i], routes[b][j]
                    left, right = routes[a][i-1], routes[a][i+1]
                    first_delta = matrix[left, second]+matrix[second, right]-matrix[left, first]-matrix[first, right]
                    left, right = routes[b][j-1], routes[b][j+1]
                    second_delta = matrix[left, first]+matrix[first, right]-matrix[left, second]-matrix[second, right]
                    updates = [(a, scores.stages[a][i-1], first_delta/teams[a]['speedMps']+service[a][second-1]-service[a][first-1]),
                               (b, scores.stages[b][j-1], second_delta/teams[b]['speedMps']+service[b][first-1]-service[b][second-1])]
                    if improves(scores.changes(first_delta+second_delta, updates), old):
                        routes[a][i], routes[b][j], changed = second, first, True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            break
    return routes


def class_search(matrix, teams, service, objective, mandatory, ordered, deadline, incumbent=None):
    common = tuple(ordered) if ordered else nearest_order(matrix, mandatory)
    started = time.perf_counter()
    best = initial_routes(matrix, teams, service, objective, common)
    build_time = time.perf_counter()-started
    def score(routes):
        return RouteScores(matrix, routes, teams, service, objective, mandatory).score()
    best_score = score(best)
    if incumbent is not None and improves(score(incumbent), best_score):
        best = [list(route) for route in incumbent]
        best_score = score(best)
    best = improve_routes(matrix, best, teams, service, objective, mandatory,
                          time.perf_counter()+max(0, deadline-time.perf_counter())*.5)
    best_score = score(best)
    alternatives = []
    if len(common) > 1 and not ordered:
        if len(common) <= 4:
            orders = set(itertools.permutations(common))
        else:
            orders = {common[::-1]}
            for i in range(len(common)-1):
                candidate = list(common)
                candidate[i], candidate[i+1] = candidate[i+1], candidate[i]
                orders.add(tuple(candidate))
        orders.discard(common)
        def tour_cost(order):
            route = (0, *order, len(matrix)-1)
            return sum(matrix[a, b] for a, b in zip(route, route[1:])), order
        alternatives.extend((order, 'far') for order in sorted(orders, key=tour_cost)[:24])
    alternatives.append((common, 'near'))
    if not common:
        alternatives.append((common, 'matched'))
        alternatives.append((common, 'spread'))
    for i, (order, policy) in enumerate(alternatives):
        remaining = deadline-time.perf_counter()
        if remaining <= max(.0001, build_time*2):
            break
        candidate = initial_routes(matrix, teams, service, objective, order, policy)
        allowance = max(0, deadline-time.perf_counter())/(len(alternatives)-i+1)
        candidate = improve_routes(matrix, candidate, teams, service, objective, mandatory, time.perf_counter()+allowance)
        value = score(candidate)
        if improves(value, best_score):
            best, best_score = candidate, value
    return improve_routes(matrix, best, teams, service, objective, mandatory, deadline)
