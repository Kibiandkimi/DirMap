"""Exact small-course solvers and honestly labelled bounded heuristics."""

import itertools
import math
import time

import numpy as np

from .exact import optimize as optimize_exact
from .schedule import custom_sequences, timeline
from .route_scores import RouteScores, improves


def balanced_people(total, count):
    if not isinstance(total, int) or not isinstance(count, int) or total < 1 or count < 1 or count > total:
        raise ValueError("人数和队数必须是正整数，队数不能超过人数")
    return [total//count+(index < total % count) for index in range(count)]


def subset_tours(matrix, deadline=math.inf, ordered_nodes=()):
    """Held-Karp for every subset, with distinct start and finish support."""
    n = len(matrix)-2
    size = 1 << n
    values = np.full((size, n), np.inf)
    parent = np.full((size, n), -1, np.int16)
    costs = np.full(size, np.inf)
    end = np.full(size, -1, np.int16)
    costs[0] = matrix[0, -1]
    required_before = [0]*n
    before = 0
    for node in ordered_nodes:
        required_before[node-1] = before
        before |= 1 << (node-1)
    for last in range(n):
        if not required_before[last]:
            values[1 << last, last] = matrix[0, last+1]
    for mask in range(1, size):
        if mask % 128 == 0 and time.perf_counter() >= deadline:
            raise TimeoutError('精确动态规划达到时间预算')
        active = [i for i in range(n) if mask & (1 << i)]
        for last in active:
            previous_mask = mask ^ (1 << last)
            if previous_mask & required_before[last] != required_before[last]:
                continue
            if previous_mask:
                options = values[previous_mask] + matrix[1:n+1, last+1]
                previous = int(np.argmin(options))
                values[mask, last] = options[previous]
                parent[mask, last] = previous
            total = values[mask, last]+matrix[last+1, -1]
            if total < costs[mask]:
                costs[mask], end[mask] = total, last

    def route(mask):
        sequence = [n+1]
        last = int(end[mask])
        while mask:
            sequence.append(last+1)
            previous = int(parent[mask, last])
            mask ^= 1 << last
            last = previous
        return [0]+list(reversed(sequence))
    return costs, route


def assign_subsets(matrix, teams, service, objective, deadline, mandatory_nodes=(), ordered_nodes=(), prepared_tours=None):
    """Two separate DP passes for a lexicographic makespan/sum objective."""
    all_costs, reconstruct = prepared_tours if prepared_tours is not None else subset_tours(matrix, deadline, ordered_nodes)
    n, k = len(matrix)-2, len(teams)
    subset_service = np.zeros((k, len(all_costs)))
    for mask in range(1, len(all_costs)):
        bit = mask & -mask
        subset_service[:, mask] = subset_service[:, mask ^ bit]+np.asarray(service)[:, bit.bit_length()-1]
    mandatory_mask = sum(1 << (node-1) for node in mandatory_nodes)
    shared = [node for node in range(1, n+1) if node not in mandatory_nodes]
    size = 1 << len(shared)
    expanded = np.full(size, mandatory_mask, np.int32)
    for mask in range(1, size):
        bit = mask & -mask
        expanded[mask] = expanded[mask ^ bit] | (1 << (shared[bit.bit_length()-1]-1))
    costs = all_costs[expanded]
    durations = np.array([costs/t['speedMps']+subset_service[i, expanded]+t.get('departureSec', 0) for i, t in enumerate(teams)])
    allow_empty = bool(mandatory_nodes)  # every team still visits all mandatory points
    if objective == 'min_max_finish_then_sum':
        primary = np.full((k+1, size), np.inf)
        primary[0, 0] = 0
        for team in range(k):
            for mask in range(0 if allow_empty else 1, size):
                if mask % 64 == 0 and time.perf_counter() >= deadline:
                    raise TimeoutError('分队动态规划达到时间预算')
                sub = mask
                while True:
                    if sub or allow_empty:
                        value = max(primary[team, mask ^ sub], durations[team, sub])
                        if value < primary[team+1, mask]:
                            primary[team+1, mask] = value
                    if not sub:
                        break
                    sub = (sub-1) & mask
        limit = primary[k, -1]
    else:
        limit = math.inf
    dp = np.full((k+1, size), np.inf)
    previous = np.zeros((k+1, size), np.int32)
    dp[0, 0] = 0
    for team in range(k):
        for mask in range(0 if allow_empty else 1, size):
            if mask % 64 == 0 and time.perf_counter() >= deadline:
                raise TimeoutError('分队动态规划达到时间预算')
            sub = mask
            while True:
                if (sub or allow_empty) and durations[team, sub] <= limit+1e-8:
                    add = costs[sub] if objective == 'min_distance' else durations[team, sub]
                    value = dp[team, mask ^ sub]+add
                    if value < dp[team+1, mask]:
                        dp[team+1, mask], previous[team+1, mask] = value, sub
                if not sub:
                    break
                sub = (sub-1) & mask
    if not math.isfinite(dp[k, -1]):
        raise ValueError('没有满足分担规则的路线')
    masks, remaining = [], size-1
    for team in range(k, 0, -1):
        mask = int(previous[team, remaining])
        masks.append(mask)
        remaining ^= mask
    return [reconstruct(int(expanded[mask])) for mask in reversed(masks)]


def route_cost(matrix, sequence):
    return float(sum(matrix[a, b] for a, b in zip(sequence, sequence[1:])))


def improve_order(matrix, sequence, deadline, ordered_nodes=()):
    best = list(sequence)
    cost = route_cost(matrix, best)
    changed = True
    while changed and time.perf_counter() < deadline:
        changed = False
        for i in range(1, len(best)-2):
            for j in range(i+1, len(best)-1):
                # Works for asymmetric costs as well as the current grid.
                candidate = best[:i]+list(reversed(best[i:j+1]))+best[j+1:]
                if ordered_nodes and [v for v in candidate if v in ordered_nodes] != list(ordered_nodes):
                    continue
                value = route_cost(matrix, candidate)
                if value < cost-1e-8:
                    best, cost, changed = candidate, value, True
            if time.perf_counter() >= deadline:
                break
    return best


def greedy_route(matrix, targets, deadline):
    remaining = set(targets)
    sequence = [0]
    while remaining:
        node = min(remaining, key=lambda n: (matrix[sequence[-1], n], n))
        remaining.remove(node)
        sequence.append(node)
    sequence.append(len(matrix)-1)
    return improve_order(matrix, sequence, deadline)


def solve(matrix, controls, teams, mode='all_visit', objective='min_max_finish_then_sum', time_limit=5,
          mandatory_ids=None, mandatory_order=False, custom_routes=None, *, use_exact=True, prepared_tours=None,
          initial_sequences=None):
    started = time.perf_counter()
    if not math.isfinite(time_limit) or not 0 < time_limit <= 30:
        raise ValueError('求解预算必须大于零且不超过 30 秒')
    deadline = started+time_limit
    matrix = np.asarray(matrix, dtype=float)
    n, k = len(controls), len(teams)
    if matrix.shape != (n+2, n+2) or (matrix < 0).any() or not np.isfinite(matrix).all():
        raise ValueError("必到点之间存在不可达路段或无效代价")
    if mode not in ('all_visit', 'split_coverage', 'fixed_order', 'mixed_coverage', 'custom'):
        raise ValueError("课程模式无效")
    if objective not in ('min_max_finish_then_sum', 'min_total_time', 'min_distance'):
        raise ValueError("目标函数无效")
    if not n or not k or (mode == 'split_coverage' and k > n):
        raise ValueError("每队至少需要一个目标，分担模式下队数不能超过目标数")
    mandatory_ids = list(mandatory_ids or [])
    indexes = {c['id']: i+1 for i, c in enumerate(controls)}
    if len(indexes) != n or len(set(mandatory_ids)) != len(mandatory_ids) or any(c not in indexes for c in mandatory_ids):
        raise ValueError('每队必到点必须是已选择的唯一目标')
    if mode == 'mixed_coverage' and not mandatory_ids:
        raise ValueError('混合模式至少需要一个每队必到点')
    if mode not in ('mixed_coverage', 'custom') and (mandatory_ids or mandatory_order):
        raise ValueError('集合点设置仅用于混合或自定义模式')
    if mode != 'custom' and custom_routes:
        raise ValueError('自定义路线仅用于自定义模式')
    mandatory_nodes = tuple(indexes[c] for c in mandatory_ids)
    ordered_nodes = mandatory_nodes if mandatory_order else ()
    for team in teams:
        if not isinstance(team['people'], int) or team['people'] < 1 or not math.isfinite(team['speedMps']) or not .2 <= team['speedMps'] <= 10:
            raise ValueError("队伍人数或速度无效")
        if not math.isfinite(team.get('departureSec', 0)) or team.get('departureSec', 0) < 0:
            raise ValueError("出发时间无效")
    service = [[float(c.get('serviceBaseSec', 5)+team['people']*c.get('servicePerPersonSec', 1)) for c in controls] for team in teams]
    if not np.isfinite(service).all() or np.any(np.asarray(service) < 0):
        raise ValueError("打点时长必须非负")
    travel_times = [matrix/t['speedMps'] for t in teams]
    departures = [t.get('departureSec', 0) for t in teams]

    def totals(sequences):
        travel = [route_cost(matrix, sequence) for sequence in sequences]
        times = timeline(sequences, travel_times, service, departures, mandatory_nodes)['finishSec']
        if objective == 'min_max_finish_then_sum':
            return (max(times), sum(times))
        return (sum(travel),) if objective == 'min_distance' else (sum(times),)

    proven, sequences, proof_details = True, None, {}
    if mode == 'custom':
        sequences = custom_sequences([c['id'] for c in controls], [t['id'] for t in teams],
                                     custom_routes or [], mandatory_ids, mandatory_order)
        proven = False
        algorithm = 'dijkstra_custom_order'
    elif mode == 'fixed_order':
        sequences = [list(range(n+2)) for _ in teams]
        algorithm = 'dijkstra_fixed_order'
    elif use_exact and mode == 'all_visit' and n <= 13:
        try:
            costs, reconstruct = prepared_tours if prepared_tours is not None else subset_tours(matrix, deadline)
            sequences = [reconstruct((1 << n)-1) for _ in teams]
            algorithm = 'held_karp'
        except TimeoutError:
            pass
    elif use_exact and mode == 'split_coverage' and n <= 10:
        try:
            sequences = assign_subsets(matrix, teams, service, objective, deadline, prepared_tours=prepared_tours)
            algorithm = 'held_karp_subset_assignment_two_stage'
        except TimeoutError:
            pass
    if sequences is None:
        proven = False
        algorithm = 'insertion_2opt_relocation'
        # Reserve most of the requested budget for the bounded exact model.
        heuristic_deadline = min(deadline, started+max(.01, time_limit*(.2 if use_exact else 1)))
        if mode == 'all_visit':
            sequence = greedy_route(matrix, range(1, n+1), heuristic_deadline)
            sequences = [sequence.copy() for _ in teams]
        else:
            # Seed one task per team, then globally compare insertion costs
            # on the obstacle-aware matrix instead of Euclidean clustering.
            targets = sorted((i for i in range(1, n+1) if i not in mandatory_nodes), key=lambda i: -matrix[0, i])
            if mandatory_nodes:
                base = [0, *ordered_nodes, n+1] if ordered_nodes else greedy_route(matrix, mandatory_nodes, heuristic_deadline)
                sequences = [base.copy() for _ in teams]
                # A shared order prevents deadlock; the exact model can still
                # optimize this order jointly when the teacher leaves it free.
                heuristic_order = tuple(node for node in base if node in mandatory_nodes)
                pending = targets
            else:
                sequences = [[0, targets[i], n+1] for i in range(k)]
                heuristic_order = ()
                pending = targets[k:]
            for node in pending:
                best = None
                scores = RouteScores(matrix, sequences, teams, service, objective, heuristic_order)
                for team in range(k):
                    for position in range(1, len(sequences[team])):
                        a, b = sequences[team][position-1:position+1]
                        delta = matrix[a, node]+matrix[node, b]-matrix[a, b]
                        value = scores.insertion(team, scores.stages[team][position-1], delta, service[team][node-1])
                        if best is None or value < best[0]:
                            best = (value, team, position)
                sequences[best[1]].insert(best[2], node)
            if not mandatory_nodes:
                sequences = [improve_order(matrix, sequence, heuristic_deadline) for sequence in sequences]
            else:
                # A shorter individual tour can make another meeting stage
                # slower. Only accept moves that improve the whole class.
                changed = True
                while changed and time.perf_counter() < heuristic_deadline:
                    changed = False
                    scores = RouteScores(matrix, sequences, teams, service, objective, heuristic_order)
                    for team, sequence in enumerate(sequences):
                        for i in range(1, len(sequence)-2):
                            for j in range(i+1, len(sequence)-1):
                                if time.perf_counter() >= heuristic_deadline:
                                    break
                                candidate = sequence[:i]+list(reversed(sequence[i:j+1]))+sequence[j+1:]
                                if tuple(v for v in candidate if v in mandatory_nodes) != heuristic_order:
                                    continue
                                if improves(scores.replacements({team: candidate}), scores.score()):
                                    sequences[team] = candidate
                                    changed = True
                                    break
                            if changed or time.perf_counter() >= heuristic_deadline:
                                break
                        if changed or time.perf_counter() >= heuristic_deadline:
                            break
            improved = True
            while improved and time.perf_counter() < heuristic_deadline:
                improved = False
                scores = RouteScores(matrix, sequences, teams, service, objective, heuristic_order)
                old = scores.score()
                for a, b in itertools.permutations(range(k), 2):
                    if len(sequences[a]) <= 3:
                        continue
                    for i in range(1, len(sequences[a])-1):
                        if sequences[a][i] in mandatory_nodes:
                            continue
                        for j in range(1, len(sequences[b])):
                            if time.perf_counter() >= heuristic_deadline:
                                break
                            source, target = sequences[a].copy(), sequences[b].copy()
                            target.insert(j, source.pop(i))
                            value = scores.replacements({a: source, b: target})
                            if improves(value, old):
                                sequences[a], sequences[b], improved = source, target, True
                                break
                        if improved or time.perf_counter() >= heuristic_deadline:
                            break
                    if improved or time.perf_counter() >= heuristic_deadline:
                        break
        if initial_sequences is not None and totals(initial_sequences) < totals(sequences):
            sequences = [seq.copy() for seq in initial_sequences]
        exact = optimize_exact(matrix, controls, teams, mode, objective, sequences, deadline,
                               mandatory_nodes=mandatory_nodes, ordered_nodes=ordered_nodes) if use_exact else None
        if exact is not None:
            sequences, proven, proof_details = exact['sequences'], exact['proven'], exact['proof']
            algorithm = proof_details['algorithm']
    # Validate the final result after every exact/heuristic/timeout path.
    if mode in ('split_coverage', 'mixed_coverage'):
        from collections import Counter
        actual = Counter(v for route in sequences for v in route[1:-1])
        expected = Counter({v: k if v in mandatory_nodes else 1 for v in range(1, n+1)})
        if actual != expected or any(len(route[1:-1]) != len(set(route[1:-1])) or
                                    not set(mandatory_nodes).issubset(route) for route in sequences):
            raise RuntimeError('路线未满足每队必到与全班分担规则')
        if ordered_nodes and any([v for v in route if v in ordered_nodes] != list(ordered_nodes) for route in sequences):
            raise RuntimeError('路线未满足必到点顺序')
    return evaluate_routes(matrix, controls, teams, sequences, mode, objective, mandatory_ids, mandatory_order,
                           proven=proven, algorithm=algorithm, proof_details=proof_details,
                           elapsed=time.perf_counter()-started)


def evaluate_routes(matrix, controls, teams, sequences, mode, objective, mandatory_ids=(), mandatory_order=False,
                    *, proven=False, algorithm='evaluated_order', proof_details=None, elapsed=0):
    """Score known legal visit orders, also used for shared-tour comparisons."""
    n = len(controls)
    mandatory_ids = list(mandatory_ids)
    indexes = {c['id']: i+1 for i, c in enumerate(controls)}
    mandatory_nodes = tuple(indexes[c] for c in mandatory_ids)
    travel_times = [matrix/t['speedMps'] for t in teams]
    service = [[float(c.get('serviceBaseSec', 5)+t['people']*c.get('servicePerPersonSec', 1)) for c in controls] for t in teams]
    departures = [t.get('departureSec', 0) for t in teams]
    routes = []
    schedule = timeline(sequences, travel_times, service, departures, mandatory_nodes)
    for index, (team, sequence) in enumerate(zip(teams, sequences)):
        travel = route_cost(matrix, sequence)/team['speedMps']
        service_time = sum(service[index][node-1] for node in sequence[1:-1])
        wait = float(schedule['waitSec'][index])
        routes.append({'teamId': team['id'], 'people': team['people'], 'order': sequence,
                       'controlIds': [controls[node-1]['id'] for node in sequence[1:-1]],
                       'travelSec': travel, 'serviceSec': service_time, 'waitSec': wait,
                       'durationSec': travel+service_time+wait,
                       'departureSec': team.get('departureSec', 0),
                       'finishSec': float(schedule['finishSec'][index]),
                       'stops': [{**{k: v for k, v in stop.items() if k != 'node'},
                                  'controlId': controls[stop['node']-1]['id'] if stop['node'] != n+1 else 'finish'}
                                 for stop in schedule['stops'][index]]})
    finishes = schedule['finishSec']
    value = (max(finishes), sum(finishes)) if objective == 'min_max_finish_then_sum' else \
        (sum(route_cost(matrix, sequence) for sequence in sequences),) if objective == 'min_distance' else (sum(finishes),)
    return {'status': 'FIXED' if mode == 'custom' else 'OPTIMAL' if proven else 'FEASIBLE', 'routes': routes,
            'rendezvous': [{**{k: v for k, v in m.items() if k != 'node'}, 'controlId': controls[m['node']-1]['id']}
                           for m in schedule['rendezvous']],
            'coverage': {'mode': mode, 'mandatoryControlIds': mandatory_ids,
                         'mandatoryOrder': mandatory_order,
                         'synchronization': 'wait_for_all' if mandatory_ids else 'independent',
                         'rendezvousControlIds': [controls[m['node']-1]['id'] for m in schedule['rendezvous']],
                         'sharedControlIds': [c['id'] for c in controls if c['id'] not in mandatory_ids] if mode in ('split_coverage', 'mixed_coverage') else []},
            'proof': {'algorithm': algorithm, 'globalOptimalityProven': proven,
                      'objective': objective, 'objectiveValue': list(value),
                      'bestBound': list(value) if proven else None, 'relativeGap': 0. if proven else None,
                      'elapsedSec': elapsed,
                      'scope': ('按教师指定的每队点序计算合法最短路，未优化点位分配或访问顺序。' if mode == 'custom' else '')
                               +'当前图上的通行代价、打点时长、指定出发时间及集合等待；所有队伍在集合点完成打点后统一出发，不包含通道拥堵。',
                      **(proof_details or {})}}
