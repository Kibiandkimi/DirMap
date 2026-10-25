#!/usr/bin/env python3
"""Collect recorded checks, keeping unverified course/field requirements open."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import store
from dirmap.protocol import ROUTING_SCHEMA, TERRAIN_SCHEMA
from dirmap.service import implementation_signature


def main():
    root = store.ROOT
    evidence = root/'evidence/runtime'
    xml = ET.parse(evidence/'python-tests.xml')
    cases = xml.findall('.//testcase')
    failures = sum(c.find('failure') is not None or c.find('error') is not None for c in cases)
    skipped = sum(c.find('skipped') is not None for c in cases)
    heading = (evidence/'heading-navigation-tests.txt').read_text()
    heading_pass = int(re.search(r'pass (\d+)', heading)[1])
    heading_fail = int(re.search(r'fail (\d+)', heading)[1])
    browser = store.read_json(evidence/'browser-tests.json')['stats']
    real = store.read_json(evidence/'real-map-verification.json')
    regression = store.read_json(evidence/'course-regressions.json')
    fresh = store.read_json(evidence/'fresh-processing.json')
    performance = store.read_json(evidence/'planning-performance.json')
    inventory = store.read_json(evidence/'recognition-inventory.json')
    detection = store.read_json(evidence/'new-map-recognition.json', {})
    new_processing = store.read_json(evidence/'new-map-processing.json', {})
    deployment = store.read_json(evidence/'recognition-deployment.json', {})
    complete = store.read_json(evidence/'complete-course-acceptance.json')
    tls = store.read_json(evidence/'https-check.json')
    build = store.read_json(evidence/'build-check.json', {})
    build_sources = [root/name for name in build.get('sourceFiles', [])]
    build_current = bool(build.get('passed') and build_sources and all(p.is_file() for p in build_sources)
                         and hashlib.sha256(b''.join(p.read_bytes() for p in build_sources)).hexdigest() == build.get('sourceHash'))
    original = store.read_json(root/'evidence/input-manifest.json')['assets']
    sources = sorted((root/'dirmap').glob('*.py')) + sorted((root/'dirmap').glob('*.cpp'))
    sources += [root/'dirmap/schemas.json'] + sorted((root/'web/src').glob('*')) + [root/'web/sw.js']
    originals = [{'file': item['file'], 'unchanged': store.file_hash(root/item['file']) == item['sha256']} for item in original]
    versions = {m['id']: store.get_state(m['id'])['version'] for m in real['maps']}
    same_versions = all(versions[m['id']] == m['stateVersion'] for m in real['maps'])
    actual_graphs = {m['sourceHash']: m.get('graphHash') for m in real['maps'] if m.get('sourceHash')}
    pending = [{'map': m['id'], **c} for m in real['maps'] for c in m['unresolved']]
    job_cases = [c for c in cases if c.get('classname', '').endswith('test_jobs') or 'completed_http_submission' in c.get('name', '')]
    job_report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'source': 'evidence/runtime/python-tests.xml',
                  'scope': 'Durable queue, live worker ownership, real process termination at commit boundaries, concurrent API writers, and HTTP idempotency.',
                  'cases': [{'name': c.get('name'), 'passed': not any(c.find(tag) is not None for tag in ('failure', 'error', 'skipped')),
                             'durationSec': float(c.get('time', 0))} for c in job_cases]}
    store.atomic_json(evidence/'job-recovery.json', job_report)
    paths = [p for p in root.rglob('*') if p.is_file() and 'node_modules' not in p.parts]
    report = {
        'auditedAt': datetime.now(timezone.utc).isoformat(), 'goalStatus': 'active', 'fullObjectiveAchieved': False,
        'routingSchema': ROUTING_SCHEMA, 'terrainSchema': TERRAIN_SCHEMA,
        'tests': {
            'python': {'passed': len(cases)-failures-skipped, 'failed': failures, 'skipped': skipped,
                       'report': 'evidence/runtime/python-tests.xml'},
            'headingAndNavigation': {'passed': heading_pass, 'failed': heading_fail,
                                     'report': 'evidence/runtime/heading-navigation-tests.txt'},
            'browser': {'passed': browser['expected'], 'failed': browser['unexpected'], 'skipped': browser['skipped'],
                        'flaky': browser['flaky'], 'durationSec': round(browser['duration']/1000, 2),
                        'report': 'evidence/runtime/browser-tests.json'},
            'totalPassed': len(cases)-failures-skipped+heading_pass+browser['expected']},
        'build': {'passed': build.get('passed'), 'matchesCurrentSources': build_current,
                  'report': 'evidence/runtime/build-check.json', 'productionEntry': 'http://localhost:8765',
                  'productionHealth': deployment.get('health', tls.get('httpHealth')),
                  'assets': [p.name for p in sorted((root/'web/dist/assets').glob('*'))]},
        'recognition': {'file': 'evidence/runtime/recognition-inventory.json',
                        'matchedControlLabels': sum(m['detectedControls'] for m in inventory['maps']),
                        'startFinishSymbols': len(inventory['maps']), 'independentCircleCentreBenchmark': False},
        'newMapRecognition': {'file': 'evidence/runtime/new-map-recognition.json',
                              'allChecksPassed': detection.get('allChecksPassed', False),
                              'distinctMaps': detection.get('distinctMapCount', 0),
                              'cases': len(detection.get('cases', [])),
                              'savedTeacherControlsUsed': detection.get('savedTeacherControlsUsed'),
                              'scope': detection.get('scope')},
        'newMapProcessing': {'file': 'evidence/runtime/new-map-processing.json',
                             'allChecksPassed': new_processing.get('allChecksPassed', False),
                             'calibratedControls': new_processing.get('calibratedControls'),
                             'reusedTeacherControls': new_processing.get('reusedTeacherControls'),
                             'sourceImage': new_processing.get('sourceImage'),
                             'scope': new_processing.get('scope')},
        'recognitionDeployment': {'file': 'evidence/runtime/recognition-deployment.json',
                                  'passed': deployment.get('passed', False),
                                  'maps': deployment.get('maps', []),
                                  'teacher43': deployment.get('teacher43')},
        'freshProcessing': {'file': 'evidence/runtime/fresh-processing.json',
                            'automaticSmokePassed': fresh['automaticRecognitionSmokePassed'],
                            'auditedAt': fresh['auditedAt'],
                            'matchesCurrentImplementation': fresh.get('implementationSignature') == implementation_signature(),
                            'liveProjectWriteGuardPassed': fresh['liveProjectWriteGuardPassed'],
                            'concurrentWorkbenchUpdatesDetected': fresh['concurrentWorkbenchUpdatesDetected'],
                            'preprocessingSec': {m['assetId']: m['processingElapsedSec'] for m in fresh['maps']},
                            'scope': fresh['scope']},
        'realMapEvidence': {'file': 'evidence/runtime/real-map-verification.json',
                            'routeScenarios': sum(len(m['routeChecks']) for m in real['maps']),
                            'fullyRoutableDetectedCourses': [m['id'] for m in real['maps'] if m['fullCourseAutomaticallyRoutable']],
                            'pendingControls': pending,
                            'currentVersions': versions, 'evidenceMatchesCurrentVersions': same_versions,
                            'allMapsAutomaticallyRoutable': all(m['fullCourseAutomaticallyRoutable'] for m in real['maps'])},
        'courseRegression': {'file': 'evidence/runtime/course-regressions.json', 'allChecksPassed': regression['allChecksPassed'],
                             'scenarios': len(regression['scenarios']), 'regions': len(regression['regions']),
                             'uniqueLegalEdgesChecked': regression['uniqueLegalEdgesChecked'],
                             'currentVersionMatched': regression['stateVersion'] == versions['szu-3']},
        'completeCourseAcceptance': {'file': 'evidence/runtime/complete-course-acceptance.json',
                                     'allRoutable': complete['allRoutable'], 'expectedExitCode': 1,
                                     'knownInputConflict': 'szu-2 / 35: 按用户已确认规则保留禁入',
                                     'additionalUnresolvedControls': [c for c in pending if not (c['map'] == 'szu-2' and c.get('label') == '35')]},
        'persistentJobs': {'file': 'evidence/runtime/job-recovery.json', 'passed': sum(c['passed'] for c in job_report['cases']),
                           'cases': len(job_cases), 'browserRecoveryCases': 2},
        'performance': {'file': 'evidence/runtime/planning-performance.json', 'controls': 30, 'teams': 10,
                        'auditedAt': performance['auditedAt'],
                        'matchesCurrentGraph': performance.get('graphHash') == actual_graphs.get(performance.get('sourceHash')),
                        'cases': [{k: c[k] for k in ('cache', 'totalPlanningSec', 'underFiveSeconds', 'status')} for c in performance['cases']],
                        'scope': performance['cacheScope'], 'syntheticControlsOnRealGraph': True},
        'https': {'file': 'evidence/runtime/https-check.json', 'customCATrustVerified': tls['customCATrustVerified'],
                  'physicalPhoneVerified': False},
        'unverified': ['独立同名点与现场配准精度', '独立圈心、禁区和门洞识别基准',
                       '全符号语义及多层通道，含楼梯分类歧义', '动态容量、时窗和跨队拥堵模型',
                       'iOS/Android 真机传感器与校园完整试走'],
        'originals': originals, 'originalHashesUnchanged': all(a['unchanged'] for a in originals),
        'implementationHash': hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest(),
        'disk': {'workspaceBytes': sum(p.stat().st_size for p in paths), 'dataBytes': sum(p.stat().st_size for p in paths if p.is_relative_to(store.DATA)),
                 'freeWorkspaceMiB': round(shutil.disk_usage(root).free/1024**2, 1), 'graphCacheLimitMiB': 256,
                 'dependencyLocations': ['/tmp/dirmap-python', '/tmp/dirmap-node', '/tmp/dirmap-ocr', '/tmp/dirmap-cache']},
        'codespaceUsed': False}
    store.atomic_json(evidence/'verification-summary.json', report)
    print({'tests': report['tests']['totalPassed'], 'failures': failures+heading_fail+browser['unexpected'],
           'currentVersionsMatched': same_versions, 'fullObjectiveAchieved': False})


if __name__ == '__main__':
    main()
