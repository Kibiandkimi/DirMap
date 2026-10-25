#!/usr/bin/env python3
"""Small synthetic design experiments, NOT an image processing application.

Pure Python, deterministic fixtures, no package installation or network access.
The mesh fixture is piecewise affine by construction: it verifies implementation
properties and cannot establish registration accuracy on the supplied maps.
"""

import argparse
from datetime import datetime, timezone
import heapq
import itertools
import json
import math
from pathlib import Path
import resource
import sys
import time


def solve(matrix, rhs):
    n = len(rhs)
    augmented = [list(row) + [value] for row, value in zip(matrix, rhs)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            raise ValueError("degenerate control geometry")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(n):
            if row != col:
                scale = augmented[row][col]
                augmented[row] = [a - scale * b for a, b in zip(augmented[row], augmented[col])]
    return tuple(row[-1] for row in augmented)


def fit_affine(pairs):
    if len(pairs) < 3:
        raise ValueError("at least three controls required")
    features = [(point[0], point[1], 1.0) for point, _ in pairs]
    normal = [[sum(row[i] * row[j] for row in features) for j in range(3)] for i in range(3)]
    return tuple(solve(normal, [sum(row[j] * target[axis] for row, (_, target)
                                       in zip(features, pairs)) for j in range(3)]) for axis in range(2))


def apply_affine(model, point):
    return tuple(row[0] * point[0] + row[1] * point[1] + row[2] for row in model)


def robust_affine(pairs, tolerance):
    # Exhaustive minimal subsets replace randomized RANSAC for this tiny fixture.
    # Production must use normalized coordinates and robust library solvers.
    best = []
    for subset in itertools.combinations(pairs, 3):
        try:
            model = fit_affine(subset)
        except ValueError:
            continue
        inliers = [pair for pair in pairs if math.dist(apply_affine(model, pair[0]), pair[1]) <= tolerance]
        if len(inliers) > len(best):
            best = inliers
    if len(best) < 3:
        raise ValueError("no valid affine model")
    return fit_affine(best), len(best)


def signed_area(a, b, c):
    return ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2


def validate_mesh(source, target, triangles):
    for indexes in triangles:
        a, b, c = (source[index] for index in indexes)
        d, e, f = (target[index] for index in indexes)
        # Both coordinate sets in this fixture have y UP; real image v is DOWN.
        if abs(signed_area(a, b, c)) < 1e-9 or signed_area(a, b, c) * signed_area(d, e, f) <= 0:
            raise ValueError("folded or collapsed mesh")


def mesh_point(point, source, target, triangles):
    for indexes in triangles:
        a, b, c = (source[index] for index in indexes)
        denominator = signed_area(a, b, c)
        weights = (signed_area(point, b, c) / denominator,
                   signed_area(a, point, c) / denominator,
                   signed_area(a, b, point) / denominator)
        if min(weights) >= -1e-9:
            return tuple(sum(weight * target[index][axis] for weight, index in zip(weights, indexes))
                         for axis in range(2))
    raise ValueError("outside calibrated hull")


def smooth_heading(previous, current, gain):
    if not all(math.isfinite(value) for value in (previous, current, gain)) or not 0 <= gain <= 1:
        raise ValueError("invalid heading sample")
    delta = (current - previous + 180) % 360 - 180
    return (previous + gain * delta) % 360


def make_grid(width, height, blocked, diagonal=False):
    nodes = {(x, y) for x in range(width) for y in range(height)} - set(blocked)
    graph = {node: {} for node in nodes}
    steps = ((1, 0), (-1, 0), (0, 1), (0, -1))
    if diagonal:
        steps += ((1, 1), (1, -1), (-1, 1), (-1, -1))
    for x, y in nodes:
        for dx, dy in steps:
            other = (x + dx, y + dy)
            if other not in nodes:
                continue
            if dx and dy and ((x + dx, y) not in nodes or (x, y + dy) not in nodes):
                continue  # No diagonal corner cutting.
            graph[(x, y)][other] = math.hypot(dx, dy)
    return graph


def shortest_path(graph, start, finish):
    if start not in graph or finish not in graph:
        raise ValueError("endpoint is not traversable")
    counter = itertools.count()
    queue = [(0.0, next(counter), start)]
    distance = {start: 0.0}
    parent = {}
    while queue:
        cost, _, node = heapq.heappop(queue)
        if cost != distance[node]:
            continue
        if node == finish:
            path = [node]
            while node != start:
                node = parent[node]
                path.append(node)
            return cost, list(reversed(path))
        for other, weight in graph[node].items():
            if weight < 0 or not math.isfinite(weight):
                raise ValueError("invalid edge weight")
            value = cost + weight
            if value < distance.get(other, math.inf):
                distance[other] = value
                parent[other] = node
                heapq.heappush(queue, (value, next(counter), other))
    raise ValueError("unreachable mandatory endpoint")


def route_cost(matrix, route):
    return sum(matrix[a][b] for a, b in zip(route, route[1:]))


def held_karp(matrix, targets):
    if not targets:
        return 0.0, (0, 0)
    states = {(1 << index, index): (matrix[0][node], (0, node)) for index, node in enumerate(targets)}
    for mask in range(1, 1 << len(targets)):
        for last, _ in enumerate(targets):
            if (mask, last) not in states:
                continue
            value, path = states[(mask, last)]
            for next_index, next_node in enumerate(targets):
                if mask & (1 << next_index):
                    continue
                key = (mask | (1 << next_index), next_index)
                candidate = (value + matrix[targets[last]][next_node], path + (next_node,))
                if key not in states or candidate < states[key]:
                    states[key] = candidate
    full = (1 << len(targets)) - 1
    return min((states[(full, last)][0] + matrix[node][0], states[(full, last)][1] + (0,))
               for last, node in enumerate(targets))


def exact_two_team_split(matrix, targets, speeds):
    options = []
    for mask in range(1, (1 << len(targets)) - 1):
        groups = (tuple(node for index, node in enumerate(targets) if mask & (1 << index)),
                  tuple(node for index, node in enumerate(targets) if not mask & (1 << index)))
        solved = [held_karp(matrix, group) for group in groups]
        durations = tuple(item[0] / speed for item, speed in zip(solved, speeds))
        options.append(((max(durations), sum(durations)), tuple(item[1] for item in solved), durations))
    if not options:
        raise ValueError("not enough targets for two nonempty teams")
    return min(options)


def brute_two_team_oracle(matrix, targets, speeds):
    # Independent enumeration of permutation + cut covers every ordered split.
    best = (math.inf, math.inf)
    for permutation in itertools.permutations(targets):
        for cut in range(1, len(targets)):
            routes = ((0,) + permutation[:cut] + (0,), (0,) + permutation[cut:] + (0,))
            times = [route_cost(matrix, route) / speed for route, speed in zip(routes, speeds)]
            best = min(best, (max(times), sum(times)))
    return best


def run():
    started = time.perf_counter()
    checks = []

    def check(name, condition, evidence=None):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "passed": True, "evidence": evidence})

    def rejects(name, action):
        try:
            action()
        except ValueError as error:
            check(name, True, str(error))
            return
        raise AssertionError(name + ": expected ValueError")

    source = list(itertools.product((0.0, 50.0, 100.0), repeat=2))
    truth = ((1.8, 0.12, 40.0), (-0.08, 2.1, 70.0))
    pairs = [(point, apply_affine(truth, point)) for point in source]
    pairs[-1] = (pairs[-1][0], (999.0, -200.0))
    model, inliers = robust_affine(pairs, 0.01)
    error = max(math.dist(apply_affine(model, point), apply_affine(truth, point)) for point in source)
    check("robust_affine_rejects_wrong_correspondence", inliers == 8 and error < 1e-7,
          {"inliers": inliers, "total_candidates": len(pairs), "max_error_m": error})
    rejects("collinear_anchors_rejected", lambda: fit_affine([((x, x), (2*x, 2*x)) for x in range(4)]))

    mesh_source = [(0, 0), (100, 0), (100, 100), (0, 100), (50, 50)]
    mesh_target = [(0, 0), (100, 0), (100, 100), (0, 100), (62, 42)]
    triangles = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    validate_mesh(mesh_source, mesh_target, triangles)
    global_model = fit_affine(list(zip(mesh_source, mesh_target)))
    withheld = [(50, 35), (65, 50), (50, 65), (35, 50), (20, 10), (90, 80), (10, 80), (80, 90)]
    # Independent closed-form square-pyramid displacement, not mesh_point().
    target_points = []
    for x, y in withheld:
        weight = 1 - max(abs(x - 50), abs(y - 50)) / 50
        target_points.append((x + 12 * weight, y - 8 * weight))
    global_rmse = math.sqrt(sum(math.dist(apply_affine(global_model, point), target)**2
                                for point, target in zip(withheld, target_points)) / len(withheld))
    mesh_rmse = math.sqrt(sum(math.dist(mesh_point(point, mesh_source, mesh_target, triangles), target)**2
                              for point, target in zip(withheld, target_points)) / len(withheld))
    roundtrip_error = max(math.dist(mesh_point(target, mesh_target, mesh_source, triangles), point)
                          for point, target in zip(withheld, target_points))
    check("local_mesh_represents_fixture_global_affine_does_not", global_rmse > 4 and mesh_rmse < 1e-7 and roundtrip_error < 1e-7,
          {"withheld_points": len(withheld), "global_affine_rmse_m": global_rmse,
           "mesh_rmse_m": mesh_rmse, "mesh_roundtrip_max_error_m": roundtrip_error,
           "caveat": "Piecewise-affine truth by construction; no inference about real maps."})
    rejects("folded_mesh_rejected", lambda: validate_mesh(mesh_source, mesh_target[:4] + [(150, 50)], triangles))
    rejects("outside_hull_rejected", lambda: mesh_point((120, 30), mesh_source, mesh_target, triangles))

    check("heading_wraps_across_north", smooth_heading(359, 1, 0.5) == 0 and smooth_heading(1, 359, 0.5) == 0,
          {"359_to_1_halfway_deg": smooth_heading(359, 1, 0.5)})
    rejects("nonfinite_heading_rejected", lambda: smooth_heading(10, math.nan, 0.2))
    directions = [(0, (0, -1)), (90, (1, 0)), (180, (0, 1)), (270, (-1, 0))]
    for heading, (x, y) in directions:
        angle = math.radians((-heading) % 360)
        rotated = (math.cos(angle)*x - math.sin(angle)*y, math.sin(angle)*x + math.cos(angle)*y)
        check("clockwise_map_rotation_cardinal_" + str(heading), math.dist(rotated, (0, -1)) < 1e-9,
              {"heading_deg": heading, "map_clockwise_rotation_deg": (-heading) % 360})

    blocked = {(4, y) for y in range(7) if y != 1}
    graph = make_grid(9, 7, blocked)
    start, finish, gate = (1, 5), (7, 5), (4, 1)
    cost, path = shortest_path(graph, start, finish)
    check("route_uses_gate_instead_of_crossing_wall", cost == 14 and gate in path and not set(path) & blocked,
          {"straight_line_m": math.dist(start, finish), "legal_path_m": cost, "path": path})
    rejects("closed_gate_makes_required_control_unreachable",
            lambda: shortest_path(make_grid(9, 7, blocked | {gate}), start, finish))
    rejects("diagonal_corner_cutting_rejected",
            lambda: shortest_path(make_grid(2, 2, {(1, 0), (0, 1)}, diagonal=True), (0, 0), (1, 1)))
    lower, upper = (1, 1, 0), (1, 1, 1)
    rejects("coincident_layers_do_not_create_junction", lambda: shortest_path({lower: {}, upper: {}}, lower, upper))
    check("explicit_portal_connects_layers", shortest_path({lower: {upper: 4}, upper: {lower: 4}}, lower, upper)[0] == 4)

    places = [start, (1, 1), (7, 1), finish, (2, 3), (6, 3)]
    matrix = [[shortest_path(graph, a, b)[0] for b in places] for a in places]
    targets = tuple(range(1, len(places)))
    dp_cost, dp_route = held_karp(matrix, targets)
    brute_cost = min(route_cost(matrix, (0,) + order + (0,)) for order in itertools.permutations(targets))
    check("single_team_tour_matches_exhaustive_optimum", dp_cost == brute_cost,
          {"targets": len(targets), "optimal_fixture_length_m": dp_cost, "route_indexes": dp_route})
    speeds = (1.0, 1.5)
    objective, routes, durations = exact_two_team_split(matrix, targets, speeds)
    oracle = brute_two_team_oracle(matrix, targets, speeds)
    visited = [node for route in routes for node in route[1:-1]]
    check("split_coverage_visits_each_target_exactly_once", sorted(visited) == list(targets), {"routes": routes})
    check("two_team_minmax_matches_independent_exhaustive_oracle",
          all(abs(a - b) < 1e-9 for a, b in zip(objective, oracle)),
          {"objective_seconds_max_then_sum": objective, "team_seconds": durations,
           "team_speeds_mps": speeds, "no_service_or_congestion_in_fixture": True})
    all_team_routes = [held_karp(matrix, targets)[1] for _ in speeds]
    check("all_visit_is_separate_from_split_coverage",
          all(sorted(route[1:-1]) == list(targets) for route in all_team_routes),
          {"visits_per_target": len(all_team_routes), "routes": all_team_routes})
    for route in routes:
        for a, b in zip(route, route[1:]):
            _, segment = shortest_path(graph, places[a], places[b])
            if set(segment) & blocked:
                raise AssertionError("expanded route crossed barrier")
    check("optimized_routes_expand_to_legal_graph_edges", True)

    return {"scope": "synthetic mathematical fixtures only; no supplied image pixels or phone hardware used",
            "passed": True, "check_count": len(checks), "checks": checks,
            "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                            "peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "run_at_utc": datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
