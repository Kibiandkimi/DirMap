"""Earliest team timelines, including barriers at class rendezvous points."""


def custom_sequences(control_ids, team_ids, routes, mandatory_ids=(), ordered=False):
    """Validate teacher-specified visits without silently adding or reordering."""
    if len(routes) != len(team_ids) or {r['teamId'] for r in routes} != set(team_ids):
        raise ValueError('请为每支队伍指定且仅指定一条自定义路线')
    lookup = {r['teamId']: r['controlIds'] for r in routes}
    selected, mandatory = set(control_ids), set(mandatory_ids)
    covered, common_order = set(), None
    for team in team_ids:
        visits = lookup[team]
        if not visits or len(set(visits)) != len(visits):
            raise ValueError(f'{team} 的自定义路线不能为空或重复经过同一目标')
        if not set(visits).issubset(selected):
            raise ValueError(f'{team} 的路线包含未选择或不存在的目标')
        if not mandatory.issubset(visits):
            raise ValueError(f'{team} 的路线遗漏了集合点，请补齐每队必到点')
        order = [c for c in visits if c in mandatory]
        if ordered and order != list(mandatory_ids):
            raise ValueError(f'{team} 的集合点顺序与指定顺序不一致')
        if common_order is not None and order != common_order:
            raise ValueError('各队的集合点顺序必须一致，否则会互相等待而无法出发')
        common_order = order
        covered.update(visits)
    if covered != selected:
        raise ValueError('自定义路线尚未覆盖全部已选择目标，请分配遗漏点或取消选择')
    indexes = {c: i+1 for i, c in enumerate(control_ids)}
    return [[0, *(indexes[c] for c in lookup[t]), len(control_ids)+1] for t in team_ids]


def timeline(sequences, travel, service, departures, mandatory_nodes=()):
    """Visit times in any consistent unit (seconds or integer milliseconds).

    Each team completes its own checkpoint service on arrival. All teams
    leave a rendezvous together after the last team is ready. Independent
    visits can be inserted between rendezvous, but their order must agree.
    This function never changes a route or invents travel through obstacles.
    """
    mandatory = set(mandatory_nodes)
    orders = [[node for node in route[1:-1] if node in mandatory] for route in sequences]
    order = orders[0] if orders else []
    if any(set(v) != mandatory or v != order for v in orders):
        raise ValueError('各队必须以相同顺序经过全部集合点，避免循环等待')
    clocks = list(departures)
    positions = [0]*len(sequences)
    stops = [[] for _ in sequences]
    waits = [0]*len(sequences)
    meetings = []
    for barrier in [*order, None]:
        for team, route in enumerate(sequences):
            while positions[team] < len(route)-1:
                a, b = route[positions[team]], route[positions[team]+1]
                arrival = clocks[team]+travel[team][a, b]
                dwell = 0 if positions[team]+1 == len(route)-1 else service[team][b-1]
                clocks[team] = arrival+dwell
                stops[team].append({'node': b, 'arrivalSec': float(arrival), 'serviceSec': float(dwell),
                                    'readySec': float(clocks[team]), 'departureSec': float(clocks[team]),
                                    'waitSec': 0., 'rendezvous': b in mandatory})
                positions[team] += 1
                if b == barrier:
                    break
        if barrier is not None:
            release = max(clocks)
            meetings.append({'node': barrier, 'allArrivedSec': max(s[-1]['arrivalSec'] for s in stops),
                             'departureSec': float(release)})
            for team in range(len(sequences)):
                wait = release-clocks[team]
                waits[team] += wait
                stops[team][-1].update(waitSec=float(wait), departureSec=float(release))
                clocks[team] = release
    return {'stops': stops, 'waitSec': waits, 'finishSec': clocks, 'rendezvous': meetings}


def apply_schedule(routes, controls, teams, mandatory_ids=()):
    """Recompute the public timeline from the actual expanded route legs."""
    import numpy as np
    ids = {c['id']: i+1 for i, c in enumerate(controls)}
    count = len(controls)+2
    sequences, travel = [], []
    for route, team in zip(routes, teams):
        seq = [0, *(ids[c] for c in route['controlIds']), count-1]
        costs = np.zeros((count, count))
        for (a, b), leg in zip(zip(seq, seq[1:]), route['legs']):
            costs[a, b] = leg['travelWeightM']/team['speedMps']
        sequences.append(seq)
        travel.append(costs)
    services = [[c.get('serviceBaseSec', 5)+t['people']*c.get('servicePerPersonSec', 1)
                 for c in controls] for t in teams]
    result = timeline(sequences, travel, services, [t.get('departureSec', 0) for t in teams],
                      [ids[c] for c in mandatory_ids])
    for i, route in enumerate(routes):
        route['stops'] = [{**{k: v for k, v in stop.items() if k != 'node'},
                           'controlId': controls[stop['node']-1]['id'] if stop['node'] != count-1 else 'finish'}
                          for stop in result['stops'][i]]
        route['waitSec'] = float(result['waitSec'][i])
        route['finishSec'] = float(result['finishSec'][i])
        route['durationSec'] = route['finishSec']-route['departureSec']
    return [{**{k: v for k, v in meeting.items() if k != 'node'}, 'controlId': controls[meeting['node']-1]['id']}
            for meeting in result['rendezvous']]
