"""Time-bounded circuit optimization over the actual obstacle-aware matrix.

CP-SAT proves optimality only for the explicitly quantized static model.
An existing feasible route survives a timeout, including model construction.
"""

import math
import time

import numpy as np
from ortools.sat.python import cp_model

from .schedule import timeline
from .route_scores import RouteScores, improves


def _circuit(model, count, optional=False):
    last = count-1
    arcs = {(i, j): model.new_bool_var(f'arc_{i}_{j}')
            for i in range(last) for j in range(1, count) if i != j}
    visits = {i: model.new_bool_var(f'visit_{i}') for i in range(1, last)} if optional else {}
    circuit = [(i, j, literal) for (i, j), literal in arcs.items()]
    circuit.append((last, 0, 1))  # virtual closure for distinct start/finish
    circuit += [(i, i, selected.Not()) for i, selected in visits.items()]
    model.add_circuit(circuit)
    if optional:
        model.add(sum(visits.values()) >= 1)
    return arcs, visits


def _hint(model, arcs, visits, sequence):
    edges, selected = set(zip(sequence, sequence[1:])), set(sequence[1:-1])
    for edge, literal in arcs.items():
        model.add_hint(literal, int(edge in edges))
    for node, literal in visits.items():
        model.add_hint(literal, int(node in selected))


def _sequence(solver, arcs, count):
    following = {a: b for (a, b), variable in arcs.items() if solver.value(variable)}
    sequence = [0]
    while sequence[-1] != count-1:
        if sequence[-1] not in following or len(sequence) > count:
            raise RuntimeError('求解器返回了不连通的路线')
        sequence.append(following[sequence[-1]])
    return sequence


def _run(model, deadline):
    remaining = deadline-time.perf_counter()
    if remaining <= .002:
        return None, cp_model.UNKNOWN
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = remaining
    solver.parameters.num_search_workers = 2
    solver.parameters.random_seed = 7
    # OR-Tools 9.15's presolve may enumerate a huge union of reachable integer
    # sums for millisecond route expressions before checking its wall timer.
    # Keep propagation/search, but disable that unbounded presolve pass.
    solver.parameters.cp_model_presolve = False
    return solver, solver.solve(model)


def optimize(matrix, controls, teams, mode, objective, seed, deadline, mandatory_nodes=(), ordered_nodes=()):
    """Improve a complete feasible incumbent and retain honest proof bounds."""
    if deadline-time.perf_counter() < .02:
        return None
    count = len(matrix)
    quantum = 1000
    model = cp_model.CpModel()
    distance = np.rint(np.asarray(matrix)*quantum).astype(np.int64)
    stages = []
    services = [[c.get('serviceBaseSec', 5)+t['people']*c.get('servicePerPersonSec', 1)
                 for c in controls] for t in teams]
    seed = [list(route) for route in seed]
    candidates = [('incumbent', seed)]

    def remember(source, routes):
        if not any(routes == previous for _, previous in candidates):
            candidates.append((source, [route.copy() for route in routes]))

    def stage(solver, status, name):
        record = {'objective': name, 'status': solver.status_name(status) if solver else 'UNKNOWN'}
        if solver:
            record.update(elapsedSec=solver.wall_time, bestBound=solver.best_objective_bound/quantum)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                record['incumbent'] = solver.objective_value/quantum
        stages.append(record)

    if mode == 'all_visit':
        # Uniform per-team speeds and fixed service times mean every static
        # objective is monotone in the SAME all-point tour length. One circuit
        # is sufficient, even with different speeds, people and departures.
        arcs, visits = _circuit(model, count)
        cost = sum(int(distance[a, b])*v for (a, b), v in arcs.items())
        seed_cost = sum(int(distance[a, b]) for a, b in zip(seed[0], seed[0][1:]))
        model.add(cost <= seed_cost)
        model.minimize(cost)
        _hint(model, arcs, visits, seed[0])
        solver, status = _run(model, deadline)
        stage(solver, status, 'tour_metres')
        sequence = _sequence(solver, arcs, count) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else seed[0]
        chosen = [sequence.copy() for _ in teams]
        remember('tour_search', chosen)

        def score(value):
            times = [value/t['speedMps']+sum(s)+t.get('departureSec', 0) for t, s in zip(teams, services)]
            if objective == 'min_distance':
                return [len(teams)*value]
            return [max(times), sum(times)] if objective == 'min_max_finish_then_sum' else [sum(times)]

        def model_score(routes):
            metres = [sum(int(distance[a, b]) for a, b in zip(route, route[1:]))/quantum for route in routes]
            times = [value/t['speedMps']+sum(s)+t.get('departureSec', 0) for value, t, s in zip(metres, teams, services)]
            if objective == 'min_distance':
                return [sum(metres)]
            return [max(times), sum(times)] if objective == 'min_max_finish_then_sum' else [sum(times)]

        model_value = model_score(chosen)
        bound = score(solver.best_objective_bound/quantum) if solver and status != cp_model.UNKNOWN else None
        proven = status == cp_model.OPTIMAL
        quantization = {'distanceM': .001, 'rounding': 'nearest', 'maxTourRoundingErrorM': (count-1)*.0005}
    else:
        circuits, expressions, rounded_times, rounded_services = [], [], [], []
        departures = [int(math.ceil(t.get('departureSec', 0)*quantum)) for t in teams]
        # A class can wait at several points. Summing all possible team work
        # bounds even a sequence whose slowest team differs at every stage.
        horizon = max(departures)+sum((count-1)*int(np.ceil(np.max(matrix)/t['speedMps']*quantum))
                                     +sum(int(math.ceil(s*quantum)) for s in svc)
                                     for t, svc in zip(teams, services))+1
        meeting_departures = {i: model.new_int_var(0, horizon, f'meeting_{i}') for i in mandatory_nodes}
        meeting_ready = {i: [] for i in mandatory_nodes}
        shared_order = {(a, b): model.new_bool_var(f'meeting_order_{a}_{b}')
                        for index, a in enumerate(mandatory_nodes) for b in mandatory_nodes[index+1:]}
        for team_index, (team, service) in enumerate(zip(teams, services)):
            arcs, visits = _circuit(model, count, optional=True)
            circuits.append((arcs, visits))
            if ordered_nodes or mandatory_nodes:
                rank = {i: model.new_int_var(0, count-1, f'visit_rank_{i}') for i in visits}
                for i, visit in visits.items():
                    model.add(rank[i] == 0).only_enforce_if(visit.Not())
                for (a, b), arc in arcs.items():
                    if b != count-1:
                        model.add(rank[b] >= (rank[a] if a else 0)+1).only_enforce_if(arc)
                for a, b in zip(ordered_nodes, ordered_nodes[1:]):
                    model.add(rank[a] < rank[b])
                for (a, b), before in shared_order.items():
                    model.add(rank[a] < rank[b]).only_enforce_if(before)
                    model.add(rank[a] > rank[b]).only_enforce_if(before.Not())
            # Round elapsed time UP for a conservative millisecond model.
            travel = np.ceil(np.asarray(matrix)/team['speedMps']*quantum).astype(np.int64)
            dwell = np.ceil(np.asarray(service)*quantum).astype(np.int64)
            rounded_times.append(travel)
            rounded_services.append(dwell)
            if mandatory_nodes:
                arrival = {i: model.new_int_var(0, horizon, f'arrival_{team_index}_{i}') for i in range(1, count)}
                leave = {0: departures[team_index]}
                for node, visit in visits.items():
                    if node in meeting_departures:
                        leave[node] = meeting_departures[node]
                        meeting_ready[node].append(arrival[node]+int(dwell[node-1]))
                    else:
                        leave[node] = arrival[node]+int(dwell[node-1])
                        model.add(arrival[node] == 0).only_enforce_if(visit.Not())
                for (a, b), arc in arcs.items():
                    model.add(arrival[b] == leave[a]+int(travel[a, b])).only_enforce_if(arc)
                expressions.append(arrival[count-1])
            else:
                expressions.append(sum(int(travel[a, b])*v for (a, b), v in arcs.items())
                                   +sum(int(dwell[i-1])*v for i, v in visits.items())+departures[team_index])
        for node, release in meeting_departures.items():
            model.add_max_equality(release, meeting_ready[node])
        for node in range(1, count-1):
            model.add(sum(visits[node] for _, visits in circuits) == (len(teams) if node in mandatory_nodes else 1))
        # Equivalent teams may be permuted without changing the objective.
        for i in range(len(teams)-1):
            if not mandatory_nodes and all(teams[i].get(k, 0) == teams[i+1].get(k, 0) for k in ('people', 'speedMps', 'departureSec')):
                a, b = circuits[i][0], circuits[i+1][0]
                model.add(sum(j*v for (u, j), v in a.items() if u == 0)
                          < sum(j*v for (u, j), v in b.items() if u == 0))

        def score(sequences):
            values = timeline(sequences, rounded_times, rounded_services, departures, mandatory_nodes)['finishSec']
            if objective == 'min_distance':
                return [sum(int(distance[a, b]) for seq in sequences for a, b in zip(seq, seq[1:]))]
            return [max(values), sum(values)] if objective == 'min_max_finish_then_sum' else [sum(values)]

        seed_score = score(seed)
        if objective == 'min_max_finish_then_sum':
            primary = model.new_int_var(0, seed_score[0], 'makespan')
            for expression in expressions:
                model.add(primary >= expression)
            secondary = sum(expressions)
        elif objective == 'min_distance':
            primary = sum(int(distance[a, b])*v for arcs, _ in circuits for (a, b), v in arcs.items())
            secondary = None
        else:
            primary, secondary = sum(expressions), None
        model.add(primary <= seed_score[0])
        model.minimize(primary)
        for (arcs, visits), seq in zip(circuits, seed):
            _hint(model, arcs, visits, seq)
        solver, status = _run(model, deadline)
        stage(solver, status, 'makespan_seconds' if secondary is not None else 'total_metres' if objective == 'min_distance' else 'total_seconds')
        chosen = [_sequence(solver, arcs, count) for arcs, _ in circuits] if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else seed
        remember('primary_search', chosen)
        primary_optimal = status == cp_model.OPTIMAL
        bound = [solver.best_objective_bound/quantum] if solver and status != cp_model.UNKNOWN else [None]
        if secondary is not None:
            bound.append(None)
            if primary_optimal:
                primary_value = int(solver.value(primary))
                model.add(primary == primary_value)
                model.minimize(secondary)
                model.clear_hints()
                for (arcs, visits), seq in zip(circuits, chosen):
                    _hint(model, arcs, visits, seq)
                solver, status = _run(model, deadline)
                stage(solver, status, 'total_seconds_with_optimal_makespan')
                if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    chosen = [_sequence(solver, arcs, count) for arcs, _ in circuits]
                    remember('secondary_search', chosen)
                    bound[1] = solver.best_objective_bound/quantum
            else:
                status = cp_model.FEASIBLE
        proven = primary_optimal and status == cp_model.OPTIMAL
        def model_score(routes):
            return [v/quantum for v in score(routes)]
        model_value = model_score(chosen)
        quantization = {'timeSec': .001, 'timeRounding': 'ceiling', 'distanceM': .001, 'distanceRounding': 'nearest'}

    if any(s['status'] == 'MODEL_INVALID' for s in stages):
        raise RuntimeError('路线优化模型无效')
    # The seed is feasible, so infeasibility is an implementation error, not
    # evidence that the teacher's selected controls are unreachable.
    if any(s['status'] == 'INFEASIBLE' for s in stages):
        raise RuntimeError('精确模型与已有可行路线冲突')
    search_proven, search_value = proven, model_value
    if search_proven:
        bound = search_value.copy()
    # Integer rounding can reverse close decisions. Keep the best measured
    # objective among the input and every completed search stage, then attach
    # a proof only if that returned route also attains the proved model value.
    # Keep this guard outside CP-SAT: additional millisecond-sum constraints
    # can cause OR-Tools 9.15 propagation to overrun its wall-clock budget.
    source, chosen = candidates[0]
    value = RouteScores(matrix, chosen, teams, services, objective, mandatory_nodes).score()
    model_value = model_score(chosen)
    for candidate_source, candidate in candidates[1:]:
        candidate_value = RouteScores(matrix, candidate, teams, services, objective, mandatory_nodes).score()
        candidate_model = model_score(candidate)
        if improves(candidate_value, value) or (not improves(value, candidate_value) and improves(candidate_model, model_value)):
            source, chosen, value, model_value = candidate_source, candidate, candidate_value, candidate_model
    proven = search_proven and model_value == search_value
    gap = None
    if bound and all(v is None for v in bound):
        bound = None
    if proven:
        bound, gap = model_value, 0.
    elif bound and bound[0] is not None and len(model_value) == 1:
        gap = max(0., (model_value[0]-bound[0])/max(1e-9, abs(model_value[0])))
    scope = '当前通行图上的路线、打点、指定出发时间及集合等待；所有队伍在每个集合点完成打点后统一出发。精确证明限于列出的毫米/毫秒量化模型，不含通道拥堵。'
    if search_proven and not proven:
        scope += ' 已保留实际指标更好的已知路线；量化模型的最优证明属于另一方案，不能用于当前返回路线。'
    return {'sequences': chosen, 'proven': proven,
            'proof': {'algorithm': 'cp_sat_circuit_two_stage' if mode in ('split_coverage', 'mixed_coverage') else 'cp_sat_circuit',
                      'bestBound': bound, 'relativeGap': gap, 'modelObjectiveValue': model_value,
                      'searchModelObjectiveValue': search_value, 'searchProvenOptimal': search_proven,
                      'solutionSource': source,
                      'quantization': quantization, 'stages': stages,
                      'termination': 'PROVEN_OPTIMAL' if proven else 'BETTER_ACTUAL_SOLUTION_RETAINED' if search_proven else 'TIME_LIMIT',
                      'scope': scope}}
