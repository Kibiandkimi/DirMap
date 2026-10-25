"""Compare feasible team counts using one map matrix and an explicit gain policy."""
import copy
import math
import time

import numpy as np

from .optimizer import balanced_people, evaluate_routes, route_cost, solve, subset_tours
from .route_scores import improves


def team_counts(total, min_people, max_teams, controls, mode, stagger=0):
    if any(type(v) is not int for v in (total, min_people, max_teams)) or not 1 <= min_people <= total <= 500 or not 1 <= max_teams <= 30:
        raise ValueError('请填写有效总人数、每队最少人数及最多队数')
    if mode not in ('all_visit', 'fixed_order', 'split_coverage', 'mixed_coverage'):
        raise ValueError('自定义点序已确定队数；自动分队请使用其他访问方式')
    maximum = min(max_teams, total//min_people, controls if mode == 'split_coverage' else max_teams)
    if stagger:
        maximum = min(maximum, int(86400//stagger)+1)
    if maximum < 1:
        raise ValueError('没有满足人数与目标覆盖规则的分队方式')
    return list(range(1, maximum+1))


def make_teams(request, count):
    return [{'id': f'第 {index+1} 队', 'people': people, 'speedMps': request['speedMps'],
             'departureSec': index*request.get('staggerSec', 0)}
            for index, people in enumerate(balanced_people(request['totalPeople'], count))]


def choose_count(rows, objective, target):
    """Smallest count retaining target fraction of the best observed gain.

    Compare the entire range: improvements can be non-monotone, especially
    with uneven team sizes, compulsory meetings, and staggered departures.
    """
    metric = 'totalDistanceM' if objective == 'min_distance' else 'totalFinishSec' if objective == 'min_total_time' else 'latestFinishSec'
    baseline = next(row for row in rows if row['teamCount'] == 1)
    best = min(rows, key=lambda row: (row[metric], row['totalFinishSec'], row['teamCount']))
    gain = max(0., baseline[metric]-best[metric])
    tolerance = max(1e-7, abs(baseline[metric])*1e-9)
    threshold = baseline[metric]-target*gain
    chosen = baseline if gain <= tolerance else min((row for row in rows if row[metric] <= threshold+tolerance), key=lambda row: row['teamCount'])
    previous = baseline[metric]
    for row in rows:
        improvement = baseline[metric]-row[metric]
        row.update(gainValue=improvement,
                   gainPercent=100*improvement/baseline[metric] if baseline[metric] > tolerance else 0.,
                   retainedGainFraction=max(0., min(1., improvement/gain)) if gain > tolerance else 0.,
                   marginalGain=previous-row[metric], recommended=row['teamCount'] == chosen['teamCount'])
        previous = row[metric]
    return {'recommendedCount': chosen['teamCount'], 'bestMetricCount': best['teamCount'], 'metric': metric,
            'baselineValue': baseline[metric], 'bestValue': best[metric], 'recommendedValue': chosen[metric],
            'targetGainFraction': target, 'retainedGainFraction': chosen['retainedGainFraction'],
            'gainPercent': chosen['gainPercent'], 'policy': 'fewest_teams_retaining_target_gain',
            'noMeaningfulGain': gain <= tolerance}


def shared_tour_result(matrix, controls, teams, mode, objective, source):
    """All-visit/fixed-order counts share one tour; only service/departure changes."""
    order = source['routes'][0]['order']
    proven = source['proof']['globalOptimalityProven']
    result = evaluate_routes(matrix, controls, teams, [order.copy() for _ in teams], mode, objective,
        proven=proven, algorithm='shared_tour_'+source['proof']['algorithm'], elapsed=source['proof']['elapsedSec'])
    result['proof']['scope'] = source['proof']['scope']+' 相同路线在不同队数间复用；按各队实际人数、速度和出发时刻重新计时。'
    if source['proof'].get('quantization'):
        # CP-SAT all-visit proofs concern the rounded TOUR, not the original
        # single team's makespan. Recompute the model objective for each K.
        quantized = np.rint(matrix*1000)/1000
        model = evaluate_routes(quantized, controls, teams, [order.copy() for _ in teams], mode, objective)
        result['proof']['quantization'] = copy.deepcopy(source['proof']['quantization'])
        result['proof']['modelObjectiveValue'] = model['proof']['objectiveValue']
        result['proof']['bestBound'] = model['proof']['objectiveValue'] if proven else None
    result['proof']['sourceTourProof'] = copy.deepcopy(source['proof'])
    return result


def recommend_teams(matrix, controls, request, time_limit, measure, progress=lambda *args: None):
    """Return the chosen solved course plus an auditable comparison table.

    measure(result, teams) supplies physical distances and actual weighted
    travel times from the chosen graph paths. Do not label a terrain-weighted
    distance matrix as walked metres in the comparison table.
    """
    started = time.perf_counter()
    deadline = started+max(.001, time_limit)
    mode, objective = request['mode'], request['objective']
    counts = team_counts(request['totalPeople'], request.get('minPeoplePerTeam', 2), request.get('maxTeams', 30),
                         len(controls), mode, request.get('staggerSec', 0))
    target = request.get('targetGainFraction', .9)
    if not math.isfinite(target) or not .5 <= target <= 1:
        raise ValueError('收益保留比例须为 50%–100%')
    settings = {'mandatory_ids': request.get('mandatoryControlIds', []), 'mandatory_order': request.get('mandatoryOrder', False)}
    results, summaries, teams_by_count = {}, {}, {count: make_teams(request, count) for count in counts}

    def retain(count, candidate):
        metrics = measure(candidate, teams_by_count[count])
        key = (metrics['latestFinishSec'], metrics['totalFinishSec']) if objective == 'min_max_finish_then_sum' else \
              (metrics['totalDistanceM'],) if objective == 'min_distance' else (metrics['totalFinishSec'],)
        old = summaries.get(count)
        if old and improves(old['_score'], key):
            return
        results[count] = candidate
        summaries[count] = {'teamCount': count, 'teamSizes': [t['people'] for t in teams_by_count[count]],
                            'status': candidate['status'], 'optimalityProven': candidate['proof']['globalOptimalityProven'],
                            **metrics, '_score': key}

    if mode in ('all_visit', 'fixed_order'):
        progress(65, '求解可在不同队数间复用的全点路线')
        source = solve(matrix, controls, teams_by_count[1], mode, objective, max(.001, min(30., deadline-time.perf_counter()-.05)))
        for count in counts:
            retain(count, shared_tour_result(matrix, controls, teams_by_count[count], mode, objective, source))
        method = 'one_shared_tour'
    else:
        # First get a checked feasible course for EVERY count. Do not stop
        # after one disappointing adjacent K: later counts can improve again.
        for index, count in enumerate(counts):
            progress(60+15*index/max(1, len(counts)), f'比较 {count} 队的分队收益')
            retain(count, solve(matrix, controls, teams_by_count[count], mode, objective, .005,
                                use_exact=False, **settings))
        prepared = None
        if (mode == 'split_coverage' and len(controls) <= 10 and deadline-time.perf_counter() > .05
                and any(not row['optimalityProven'] for row in summaries.values())):
            try:
                prepared = subset_tours(matrix, min(deadline, time.perf_counter()+max(.03, (deadline-time.perf_counter())*.25)))
            except TimeoutError:
                pass
        # Allocate the remaining budget to every count, with the current
        # best and threshold neighbours first. Existing incumbents survive.
        provisional = choose_count([dict(summaries[c]) for c in counts], objective, target)
        leading = [provisional['bestMetricCount'], provisional['recommendedCount'], 1]
        order = [count for count in dict.fromkeys(leading+counts) if not summaries[count]['optimalityProven']]
        # Keep room to improve the route we actually return, rather than
        # spending the entire budget equally on counts the teacher won't use.
        reserve = min(5., max(0., deadline-time.perf_counter())*.25)
        comparison_deadline = deadline-reserve
        for index, count in enumerate(order):
            remaining = comparison_deadline-time.perf_counter()
            if remaining <= .015:
                break
            budget = min(30., remaining/(len(order)-index))
            progress(76+16*index/max(1, len(order)), f'细化 {count} 队的路线')
            retain(count, solve(matrix, controls, teams_by_count[count], mode, objective, budget,
                prepared_tours=prepared, initial_sequences=[r['order'] for r in results[count]['routes']], **settings))
        selected = choose_count([dict(summaries[c]) for c in counts], objective, target)['recommendedCount']
        remaining = deadline-time.perf_counter()
        if not summaries[selected]['optimalityProven'] and remaining > .02:
            progress(94, f'完善建议的 {selected} 队路线')
            retain(selected, solve(matrix, controls, teams_by_count[selected], mode, objective, min(30., remaining),
                prepared_tours=prepared, initial_sequences=[r['order'] for r in results[selected]['routes']], **settings))
        method = 'shared_matrix_and_subset_tours' if prepared else 'shared_matrix_and_incumbents'
    rows = [{k: v for k, v in summaries[count].items() if k != '_score'} for count in counts]
    decision = choose_count(rows, objective, target)
    count = decision['recommendedCount']
    result = results[count]
    result['teamRecommendation'] = {**decision, 'sourceRequest': copy.deepcopy(request), 'candidates': rows,
        'evaluatedCounts': counts, 'allCountsCompared': len(rows) == len(counts),
        'allCountsOptimalityProven': all(row['optimalityProven'] for row in rows),
        'method': method, 'elapsedSec': time.perf_counter()-started,
        'scope': '按当前优化目标比较允许的全部队数；推荐达到指定比例的已计算最佳收益的最少队数。限时可行解之间的比较不代表全局最优队数。'}
    return result, teams_by_count[count]
