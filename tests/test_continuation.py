"""Resume saved real API plans, preserve incumbents, and publish exact results."""
import copy
import itertools

import pytest

from test_api import client, wait_job, request as plan_request, choice_request, choose_plan
from dirmap import comparisons, service, store, team_planning
from dirmap.optimizer import evaluate_routes


def course_request(client, mode='all_visit'):
    state = store.get_state('szu-1')
    controls = [{'id': f'c{i+1}', 'label': str(31+i), 'pixel': pixel, 'radiusPx': 3,
                 'required': True, 'state': 'candidate', 'serviceBaseSec': 5, 'servicePerPersonSec': 1}
                for i, pixel in enumerate(([26, 26], [8, 32], [32, 8], [35, 35]))]
    response = client.patch('/api/maps/szu-1', json={'version': state['version'], 'controls': controls})
    assert response.status_code == 200, response.text
    request = plan_request(version=response.json()['version'], people=12)
    request.update(mode=mode, controlIds=[c['id'] for c in controls])
    if mode == 'custom':
        request['customRoutes'] = [{'teamId': 'team1', 'controlIds': request['controlIds'][::-1]}]
    return request


def longest_valid_route(matrix, controls, teams, mode, objective, *args, **kwargs):
    last = len(controls)+1
    order = max(itertools.permutations(range(1, last)),
                key=lambda p: sum(matrix[a, b] for a, b in zip((0, *p, last), (0, *p, last)[1:])))
    return evaluate_routes(matrix, controls, teams, [[0, *order, last] for _ in teams], mode, objective)


def initial_plan(client, monkeypatch, *, bad=False, automatic=False, mode='all_visit'):
    request = course_request(client, mode)
    real = service.solve
    def feasible(*args, **kwargs):
        kwargs['use_exact'] = False
        return real(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(team_planning if automatic else service, 'solve', longest_valid_route if bad else feasible)
        if automatic:
            request.pop('teams')
            request.update(totalPeople=12, minPeoplePerTeam=2, maxTeams=6, speedMps=1.4, staggerSec=0, targetGainFraction=.9)
        response = client.post('/api/plans/auto-teams' if automatic else '/api/plans', json=request)
        assert response.status_code == 200, response.text
        job = wait_job(client, response.json()['id'])
        assert job['state'] == 'succeeded', job
    return job['result']


def refine(client, source, key='continue-current-plan', **overrides):
    payload = {'assetId': source['assetId'], 'version': source['version'], 'planId': source['id'], 'timeLimitSec': .5, **overrides}
    response = client.post('/api/plans/refine', json=payload, headers={'X-DirMap-Request-Id': key})
    assert response.status_code == 200, response.text
    result = wait_job(client, response.json()['id'])
    assert result['state'] == 'succeeded', result
    return result['result']


def review(client, source, change_service=False):
    state = store.get_state(source['assetId'])
    editable = {'id', 'label', 'pixel', 'radiusPx', 'required', 'role', 'state', 'approachPixel', 'serviceBaseSec', 'servicePerPersonSec'}
    controls = [{k: v for k, v in c.items() if k in editable} for c in state['controls']]
    for control in controls:
        control['state'] = 'accepted'
    if change_service:
        controls[0]['serviceBaseSec'] += 20
    updated = client.patch('/api/maps/'+source['assetId'], json={'version': state['version'], 'controls': controls})
    assert updated.status_code == 200, updated.text
    version = updated.json()['version']
    checked = client.post('/api/maps/'+source['assetId']+'/review', json={'version': version,
        'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True})
    assert checked.status_code == 200, checked.text
    return version


def test_resume_uses_the_exact_incumbent_and_improves_without_changing_teams(client, monkeypatch):
    source = initial_plan(client, monkeypatch, bad=True)
    plan_path = comparisons.plan_path(source['assetId'], source['id'])
    original = plan_path.read_bytes()
    inputs_path = comparisons.path(source['assetId'], source['continuation']['id'])
    inputs = inputs_path.read_bytes()
    real, seen = service.solve, []
    def checked(*args, **kwargs):
        seen.append(copy.deepcopy(kwargs.get('initial_sequences')))
        return real(*args, **kwargs)
    monkeypatch.setattr(service, 'solve', checked)
    improved = refine(client, source)
    replay = refine(client, source)
    assert replay == improved and len(seen) == 1
    assert seen[0] == [r['order'] for r in source['routes']]
    assert improved['request']['teams'] == source['request']['teams']
    assert improved['refinement']['sourcePlanId'] == source['id']
    assert improved['refinement']['improved'] is True
    assert improved['refinement']['after']['latestFinishSec'] < improved['refinement']['before']['latestFinishSec']
    assert improved['continuation']['id'] != source['continuation']['id']
    assert plan_path.read_bytes() == original and inputs_path.read_bytes() == inputs
    assert improved['proof']['globalOptimalityProven']
    unchanged = refine(client, improved, key='already-optimal-route')
    assert not unchanged['refinement']['attempted'] and len(seen) == 1
    assert [r['geometry'] for r in unchanged['routes']] == [r['geometry'] for r in improved['routes']]


def test_worse_attempt_keeps_original_geometry_and_remains_recoverable(client, monkeypatch):
    source = initial_plan(client, monkeypatch)
    monkeypatch.setattr(service, 'solve', longest_valid_route)
    result = refine(client, source)
    assert result['refinement']['attempted'] and not result['refinement']['improved']
    assert result['refinement']['before'] == result['refinement']['after']
    assert [r['geometry'] for r in result['routes']] == [r['geometry'] for r in source['routes']]
    job = client.get('/api/jobs/'+result['id']).json()
    assert job['kind'] == 'refine_plan' and job['result'] == result


def test_refined_team_comparison_updates_one_candidate_and_can_switch_away_and_back(client, monkeypatch):
    original = initial_plan(client, monkeypatch, bad=True, automatic=True)
    selected = choose_plan(client, choice_request(original, 6), 'choose-six-before-refining')
    context_path = comparisons.path('szu-1', selected['continuation']['id'])
    before = context_path.read_bytes()
    result = refine(client, selected)
    recommendation = result['teamRecommendation']
    assert len(result['routes']) == recommendation['selectedCount'] == 6
    assert recommendation['selectionMode'] == 'teacher' and recommendation['refinedCounts'] == [6]
    assert result['refinement']['improved']
    keys = ('latestFinishSec', 'totalFinishSec', 'totalDistanceM', 'totalWaitSec')
    for old, new in zip(original['teamRecommendation']['candidates'], recommendation['candidates']):
        if new['teamCount'] != 6:
            assert {key: old[key] for key in keys} == {key: new[key] for key in keys}
    selected_row = next(row for row in recommendation['candidates'] if row['teamCount'] == 6)
    assert selected_row['latestFinishSec'] == pytest.approx(max(r['finishSec'] for r in result['routes']))
    baseline = recommendation['candidates'][0]['latestFinishSec']
    best = min(row['latestFinishSec'] for row in recommendation['candidates'])
    threshold = baseline-.9*(baseline-best)
    assert recommendation['recommendedCount'] == min(row['teamCount'] for row in recommendation['candidates'] if row['latestFinishSec'] <= threshold+1e-6)
    assert context_path.read_bytes() == before
    another = choose_plan(client, choice_request(result, 2), 'choose-two-after-refining')
    restored = choose_plan(client, choice_request(another, 6), 'return-to-refined-six')
    assert [r['geometry'] for r in restored['routes']] == [r['geometry'] for r in result['routes']]
    assert restored['refinement'] == result['refinement']


def test_review_and_publication_preserve_refined_routes_without_another_solve(client, monkeypatch):
    source = initial_plan(client, monkeypatch, bad=True)
    selected = refine(client, source)
    version = review(client, selected)
    def forbidden(*args, **kwargs):
        raise AssertionError('Review confirmation must preserve the selected route')
    monkeypatch.setattr(service, 'solve', forbidden)
    monkeypatch.setattr(service, 'recommend_teams', forbidden)
    response = client.post('/api/plans/confirm', json={'assetId': 'szu-1', 'version': version, 'planId': selected['id']})
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()['id'])
    assert job['state'] == 'succeeded', job
    confirmed = job['result']
    assert not confirmed['draft'] and confirmed['version'] == version
    assert confirmed['refinement'] == selected['refinement']
    assert [r['geometry'] for r in confirmed['routes']] == [r['geometry'] for r in selected['routes']]
    assert [r['finishSec'] for r in confirmed['routes']] == [r['finishSec'] for r in selected['routes']]
    release = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': confirmed['id']})
    assert release.status_code == 200, release.text


def test_changed_model_and_unreviewed_targets_cannot_confirm_the_old_plan(client, monkeypatch):
    source = initial_plan(client, monkeypatch)
    response = client.post('/api/plans/confirm', json={'assetId': 'szu-1', 'version': source['version'], 'planId': source['id']})
    failed = wait_job(client, response.json()['id'])
    assert failed['state'] == 'failed' and '核验' in failed['message']
    version = review(client, source, change_service=True)
    response = client.post('/api/plans/confirm', json={'assetId': 'szu-1', 'version': version, 'planId': source['id']})
    failed = wait_job(client, response.json()['id'])
    assert failed['state'] == 'failed' and '耗时设置' in failed['message']
    response = client.post('/api/plans/refine', json={'assetId': 'szu-1', 'version': version, 'planId': source['id']})
    failed = wait_job(client, response.json()['id'])
    assert failed['state'] == 'failed' and '地图已更新' in failed['message']


def test_original_plan_changes_during_refinement_are_not_overwritten(client, monkeypatch):
    source = initial_plan(client, monkeypatch)
    original = service.solve
    path = comparisons.plan_path('szu-1', source['id'])
    def change_source(*args, **kwargs):
        result = original(*args, **kwargs)
        changed = store.read_json(path)
        changed['notes'] = 'Concurrent local edit'
        store.atomic_json(path, changed)
        return result
    monkeypatch.setattr(service, 'solve', change_source)
    response = client.post('/api/plans/refine', json={'assetId': 'szu-1', 'version': source['version'], 'planId': source['id'], 'timeLimitSec': .5})
    failed = wait_job(client, response.json()['id'])
    assert failed['state'] == 'failed' and '原路线记录已变化' in failed['message']
    assert store.read_json(path)['notes'] == 'Concurrent local edit'
    assert not comparisons.plan_path('szu-1', failed['id']).exists()


@pytest.mark.parametrize('mode', ['fixed_order', 'custom'])
def test_fixed_visit_instructions_are_not_reordered_by_refinement(client, monkeypatch, mode):
    source = initial_plan(client, monkeypatch, mode=mode)
    response = client.post('/api/plans/refine', json={'assetId': 'szu-1', 'version': source['version'], 'planId': source['id']})
    failed = wait_job(client, response.json()['id'])
    assert failed['state'] == 'failed' and '固定或自定义' in failed['message']


def test_legacy_or_corrupt_inputs_require_a_new_plan(client, monkeypatch):
    source = initial_plan(client, monkeypatch)
    path = comparisons.path('szu-1', source['continuation']['id'])
    record = store.read_json(path)
    record['content']['bundle']['matrix'][0][1] += 2
    store.atomic_json(path, record)
    payload = {'assetId': 'szu-1', 'version': source['version'], 'planId': source['id']}
    response = client.post('/api/plans/refine', json=payload)
    assert response.status_code == 422 and '记录已改变' in response.text
    source.pop('continuation')
    store.atomic_json(comparisons.plan_path('szu-1', source['id']), source)
    response = client.post('/api/plans/refine', json=payload)
    assert response.status_code == 422 and '旧路线' in response.text
