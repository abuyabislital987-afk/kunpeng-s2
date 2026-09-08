#!/usr/bin/env python3
"""Reproducible candidate snapshots and measured promotion, Python standard library only."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'conv': [(4096, 6144, 39, 39), (6144, 4096, 41, 41),
             (4256, 6390, 55, 55), (6390, 4256, 81, 81)],
    'zgemm': [(7427, 7427, 256), (14848, 14848, 256), (37360, 8192, 512)],
    'trsm': [(512, 19968), (2432, 17024), (17024, 512)],
}
KERNEL = {'conv': 'conv2d.c', 'zgemm': 'zgemm.c', 'trsm': 'trsm.c'}
PREFIX = {'conv': 'C', 'zgemm': 'Z', 'trsm': 'T'}
SUFFIXES = {'.c', '.h', '.sh', '.md'}
BENCH_SHA = {
    'conv': '2548861ae7e29826e454b4c0b098d682f7f04996222e664cd1ea9bc92dd2e927',
    'zgemm': 'ef35dea06b08c58f976065215bbaaa39b254c3b69440697d73fed3e9e7f38929',
    'trsm': 'e051d897622f73093179f93c33cfcf1b5511216f286a3050b814e1dd7582d8ac',
}


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files(folder):
    files = {}
    for p in sorted(Path(folder).rglob('*')):
        if p.is_symlink():
            raise ValueError('源码快照不接受符号链接: ' + str(p))
        if p.is_file() and p.suffix in SUFFIXES:
            files[p.relative_to(folder).as_posix()] = digest(p)
    return files


def validate_id(problem, version):
    if not re.fullmatch(PREFIX[problem] + r'\d+(?:-[A-Za-z0-9]+)*', version):
        raise ValueError('版本名示例: ' + PREFIX[problem] + '1-alice')


def run_dir(problem, version):
    validate_id(problem, version)
    return ROOT / '.runs' / problem / version


def record_path(problem, version):
    validate_id(problem, version)
    return ROOT / 'records' / 'experiments' / problem / (version + '.json')


def history_version(problem, version):
    path = ROOT / 'records' / 'history.json'
    if path.exists():
        for v in read_json(path).get('versions', []):
            if v.get('id') == version and v.get('problem') == problem:
                return v
    return None


def get_record(problem, version):
    p = record_path(problem, version)
    if not p.exists():
        if history_version(problem, version):
            raise ValueError(version + ' 只有历史证据。先 new 同名版本并在当前环境重测、record、promote 建立可比较基线。')
        raise ValueError('没有已记录的版本: ' + version)
    return read_json(p)


@contextlib.contextmanager
def locked():
    folder = ROOT / '.runs'
    folder.mkdir(exist_ok=True)
    with (folder / '.workflow.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def parse_log(problem, text, repeats):
    """Accept exactly one ordered official suite per repeat; FAIL may exit zero."""
    if repeats < 1:
        raise ValueError('repeats 必须 >= 1')
    expected = CASES[problem]
    rows = []
    ndims = len(expected[0])
    for line in text.splitlines():
        if re.search(r'\bFAIL\b', line):
            raise ValueError('日志包含 FAIL')
        if not re.search(r'\bPASS\s*$', line):
            continue
        parts = line.split()
        # The final four columns have no x; dimensions may contain x7427 or x 7427.
        if len(parts) < 5:
            raise ValueError('PASS 行无法解析: ' + line)
        head = ' '.join(parts[:-4])
        pattern = (r'\s*(\d+)\s*x\s*(\d+)\s+(\d+)\s*x\s*(\d+)\s*'
                   if problem == 'conv' else r'\s*(\d+)' + r'\s*x\s*(\d+)' * (ndims - 1) + r'\s*')
        match = re.fullmatch(pattern, head)
        if not match:
            raise ValueError('PASS 行尺寸无效: ' + line)
        dims = tuple(map(int, match.groups()))
        try:
            ms, gflops, err = map(float, parts[-4:-1])
        except ValueError as exc:
            raise ValueError('PASS 行成绩不是数值') from exc
        if not all(math.isfinite(x) for x in (ms, gflops, err)) or ms <= 0 or gflops <= 0 or err < 0:
            raise ValueError('成绩必须有限且耗时/GFLOPS为正')
        rows.append({'dims': list(dims), 'time_ms': ms, 'gflops': gflops, 'max_error': err})
    if [tuple(r['dims']) for r in rows] != expected * repeats:
        raise ValueError('必须包含完整官方用例且顺序一致，每轮各一次；期望 %d 行，实际 %d 行' % (len(expected) * repeats, len(rows)))
    result = []
    for i, dims in enumerate(expected):
        samples = rows[i::len(expected)]
        times = [s['time_ms'] for s in samples]
        median = statistics.median(times)
        result.append({'dims': list(dims), 'times_ms': times,
                       'median_ms': median, 'min_ms': min(times), 'max_ms': max(times),
                       'spread_pct': (max(times) - min(times)) / median * 100,
                       'gflops': [s['gflops'] for s in samples],
                       'max_error': max(s['max_error'] for s in samples)})
    return {'cases': result, 'repeats': repeats,
            'total_median_ms': sum(c['median_ms'] for c in result),
            'official_score': None, 'ranking': None}


def scheduler_ok(text):
    """Support the observed Donau text format and normalized JSON status."""
    try:
        value = json.loads(text)
    except ValueError:
        value = {}
        for field in ('state', 'status', 'jobExitCode', 'systemExitCode'):
            m = re.search(r'^\s*' + field + r'\s*[:=]?\s+(\S+)\s*$', text, re.M)
            if m:
                value[field] = m.group(1)
    if isinstance(value, dict) and 'jobs' in value:
        jobs = value['jobs']
        value = jobs[0] if isinstance(jobs, list) and len(jobs) == 1 else {}
    if not isinstance(value, dict):
        return False
    return (value.get('state', value.get('status')) == 'SUCCEEDED'
            and str(value.get('jobExitCode')) == '0'
            and str(value.get('systemExitCode')) == '0')


def new(args):
    dest = run_dir(args.problem, args.version)
    if dest.exists() or record_path(args.problem, args.version).exists():
        raise ValueError('版本已存在，请用新的候选编号，避免覆盖实验')
    source = ROOT / args.problem
    settings = {'bench_repeats': 3}
    if args.parent:
        validate_id(args.problem, args.parent)
        if args.parent == args.version:
            raise ValueError('父版本不能是自己')
        parent_source = run_dir(args.problem, args.parent) / 'source'
        if parent_source.is_dir():
            # A source snapshot must still correspond to the record used as parent.
            parent = get_record(args.problem, args.parent)
            if source_files(parent_source) != parent['source_hashes']:
                raise ValueError('父版本源码在测量后有变化')
            source = parent_source
            settings = parent.get('settings', settings)
        elif record_path(args.problem, args.parent).exists():
            parent = get_record(args.problem, args.parent)
            if source_files(source) != parent['source_hashes']:
                raise ValueError('本地题目录与父记录不符，需同步该版本的源码')
            settings = parent.get('settings', settings)
        elif not history_version(args.problem, args.parent):
            raise ValueError('父版本不存在或其源码快照已丢失')
        else:
            historical = history_version(args.problem, args.parent)
            expected_sha = next((x['sha256'] for x in historical['source_files']
                                 if x['path'] == args.problem + '/' + KERNEL[args.problem]), None)
            if digest(source / KERNEL[args.problem]) != expected_sha:
                raise ValueError('当前题目录已经不是所选历史父版本')
    files = source_files(source)
    if KERNEL[args.problem] not in files or 'run.sh' not in files or 'bench_' + args.problem + '.c' not in files:
        raise ValueError('缺少算子、官方 benchmark 或 run.sh')
    if files['bench_' + args.problem + '.c'] != BENCH_SHA[args.problem]:
        raise ValueError('官方 benchmark 哈希不符')
    for rel in files:
        target = dest / 'source' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    meta = {'schema_version': 1, 'problem': args.problem, 'version': args.version,
            'parent': args.parent, 'strategy': args.strategy, 'created_at': now(),
            'settings': settings, 'source_hashes': files, 'status': 'planned'}
    write_json(dest / 'experiment.json', meta)
    write_json(record_path(args.problem, args.version), meta)
    print(str(dest))


def checkpoint(args):
    folder = run_dir(args.problem, args.version)
    path = record_path(args.problem, args.version)
    previous = read_json(path) if path.exists() else {}
    if previous.get('status') not in (None, 'planned', 'prepared'):
        raise ValueError('测量之后不能改写候选准备记录')
    meta = read_json(folder / 'experiment.json')
    meta.update(source_hashes=source_files(folder / 'source'), status='prepared',
                note=args.note, updated_at=now(), verified=False)
    write_json(path, meta)
    print('已保存待测候选及策略: ' + args.version)


def machine_profile(folder, log):
    profile = {}
    p = folder / 'environment.log'
    if p.exists():
        for key in ('HOST', 'ARCH', 'NUMA_NODE', 'ALLOWED_CPUS', 'OMP_NUM_THREADS', 'CPU_TARGET'):
            match = re.search(r'^' + key + r'=(.+)$', p.read_text(), re.M)
            if match:
                profile[key] = match.group(1).strip()
    # Read the actual compiler banner produced by each problem's runner.
    banners = sorted(set(line.strip() for line in log.splitlines()
                         if re.match(r'^(?:Compiler:\s*)?(?:gcc |clang version |.*clang version )', line)))
    profile['compiler_banners'] = banners
    return profile


def record(args):
    folder = run_dir(args.problem, args.version)
    path = record_path(args.problem, args.version)
    previous = read_json(path) if path.exists() else None
    if previous and previous['status'] not in ('planned', 'prepared', 'unverified'):
        raise ValueError('测量记录不可覆盖；复测请新建带后缀的版本')
    meta = read_json(folder / 'experiment.json')
    source = source_files(folder / 'source')
    evidence = folder / 'evidence'
    evidence.mkdir(exist_ok=True)
    result = dict(meta, recorded_at=now(), source_hashes=source,
                  environment=args.environment, reference=args.reference,
                  status='failed', verified=False)
    if args.failure:
        if previous and previous['status'] == 'unverified':
            raise ValueError('已有测量记录，不能覆盖为手工失败说明')
        result['failure'] = args.failure
        if (folder / 'cluster.json').exists():
            result['job_id'] = read_json(folder / 'cluster.json').get('job_id')
        if args.log and Path(args.log).is_file():
            shutil.copy2(args.log, evidence / 'benchmark.log')
            result['log_sha256'] = digest(evidence / 'benchmark.log')
            result['log'] = str((evidence / 'benchmark.log').relative_to(ROOT))
        write_json(path, result)
        print('已记录失败: ' + args.failure)
        return
    if not args.log:
        raise ValueError('需要 --log 或 --failure')
    log_path = Path(args.log).resolve()
    text = log_path.read_text(errors='replace')
    if previous and previous.get('log_sha256') and (digest(log_path) != previous['log_sha256'] or source != previous['source_hashes']):
        raise ValueError('只能为同一日志和源码补齐远程验证凭据；新测量请使用新编号')
    (evidence / 'benchmark.log').write_text(text)
    result['log_sha256'] = digest(evidence / 'benchmark.log')
    result['log'] = str((evidence / 'benchmark.log').relative_to(ROOT))
    try:
        measurements = parse_log(args.problem, text, args.repeats)
    except ValueError as exc:
        result['failure'] = str(exc)
        write_json(path, result)
        raise ValueError('无效成绩已写入失败记录: ' + str(exc)) from exc
    result.update(measurements)
    result['status'] = 'measured'
    cluster_path = folder / 'cluster.json'
    cluster = read_json(cluster_path) if cluster_path.exists() else {}
    status_path = Path(args.scheduler_status) if args.scheduler_status else folder / 'scheduler-status.txt'
    status_text = status_path.read_text() if status_path.exists() else ''
    if status_text:
        (evidence / 'scheduler-status.txt').write_text(status_text)
    wrapper_ok = (folder / 'exit-code.txt').exists() and (folder / 'exit-code.txt').read_text().strip() == '0'
    uploaded_hashes = cluster.get('source_hashes', {})
    hash_match = bool(uploaded_hashes) and uploaded_hashes == source
    remote_hash_file = folder / 'source-sha256.txt'
    remote_hashes = {}
    if remote_hash_file.exists():
        for line in remote_hash_file.read_text().splitlines():
            m = re.fullmatch(r'([0-9a-f]{64})\s+\*?(?:\./)?(?:source/)?(.+)', line)
            if m:
                remote_hashes[m.group(2)] = m.group(1)
    hash_match = hash_match and all(remote_hashes.get(k) == v for k, v in source.items())
    result['machine'] = machine_profile(folder, text)
    result['settings'] = cluster.get('settings', meta.get('settings', {}))
    result['job_id'] = cluster.get('job_id')
    job_match = re.search(r'(?m)^\s*jobId\s*[:=]?\s+(\d+)\s*$', status_text)
    if not job_match:
        job_match = re.search(r'"(?:jobId|job_id)"\s*:\s*"?(\d+)"?', status_text)
    same_job = bool(job_match) and job_match.group(1) == str(cluster.get('job_id'))
    artifacts = cluster.get('artifacts_sha256', {})
    artifacts_ok = all((folder / name).is_file() and digest(folder / name) == artifacts.get(name)
                       for name in ('benchmark.log', 'environment.log', 'exit-code.txt', 'source-sha256.txt'))
    result['checks'] = {'scheduler': scheduler_ok(status_text) and same_job,
                        'fetched_log': log_path == (folder / 'benchmark.log').resolve() and artifacts_ok,
                        'benchmark': source.get('bench_' + args.problem + '.c') == BENCH_SHA[args.problem],
                        'wrapper': wrapper_ok,
                        'source_hashes': hash_match,
                        'machine': all(k in result['machine'] for k in ('HOST', 'ARCH', 'NUMA_NODE', 'ALLOWED_CPUS', 'OMP_NUM_THREADS', 'CPU_TARGET')) and bool(result['machine']['compiler_banners'])}
    result['verified'] = all(result['checks'].values())
    result['status'] = 'passed' if result['verified'] else 'unverified'
    write_json(path, result)
    print('%s: %s, %.2f ms; 官方分数未知' % (args.version, result['status'], result['total_median_ms']))


def comparison(base, candidate):
    reasons = []
    for rec in (base, candidate):
        if not rec.get('verified'):
            reasons.append(rec['version'] + ' 缺少完整远程验证证据')
        if rec.get('repeats', 0) < 3:
            reasons.append(rec['version'] + ' 需要至少3轮独立测量')
    for field in ('problem', 'environment', 'reference', 'machine'):
        if base.get(field) != candidate.get(field):
            reasons.append(field + ' 不同，需同环境重测')
    problem = base['problem']
    bench = 'bench_' + problem + '.c'
    if base['source_hashes'].get(bench) != candidate['source_hashes'].get(bench):
        reasons.append('benchmark 有变化，拒绝比较')
    # TEST_RUNS/BLAS/compiler are evaluation conditions, while blocking knobs are tunable.
    for key in ('TEST_RUNS', 'KBLAS_LIB', 'CC', 'COMPILER'):
        a = base.get('settings', {}).get('environment', {}).get(key)
        b = candidate.get('settings', {}).get('environment', {}).get(key)
        if a != b:
            reasons.append(key + ' 不同，需同时复跑父版本')
    rows = []
    if not reasons:
        for a, b in zip(base['cases'], candidate['cases']):
            if a['dims'] != b['dims']:
                reasons.append('测试尺寸不同')
                break
            gain = (a['median_ms'] - b['median_ms']) / a['median_ms'] * 100
            rows.append({'dims': a['dims'], 'base_ms': a['median_ms'],
                         'candidate_ms': b['median_ms'], 'gain_pct': gain})
        if not reasons:
            total_gain = (base['total_median_ms'] - candidate['total_median_ms']) / base['total_median_ms'] * 100
            noise = max([1.0] + [r['spread_pct'] for r in base['cases'] + candidate['cases']])
            if total_gain <= noise:
                reasons.append('提速 %.2f%% 未超过测量波动/最低1%%门槛 %.2f%%' % (total_gain, noise))
            if any(r['gain_pct'] < -1 for r in rows):
                reasons.append('至少一组退步超过1%，需评估官方计分权重后再决定')
            if (base['source_hashes'] == candidate['source_hashes']
                    and base.get('settings') == candidate.get('settings')):
                reasons.append('源码和设置相同，只是复测，不生成提速版本')
    return {'eligible': not reasons, 'reasons': reasons, 'cases': rows,
            'metric': 'sum of per-case median ms; not official score'}


def compare(args):
    print(json.dumps(comparison(get_record(args.problem, args.base), get_record(args.problem, args.candidate)), ensure_ascii=False, indent=2))


def promote(args):
    rec = get_record(args.problem, args.version)
    folder = run_dir(args.problem, args.version)
    if not rec.get('verified') or rec.get('repeats', 0) < 3:
        raise ValueError('晋级需要远程验证完整且至少3轮')
    if source_files(folder / 'source') != rec['source_hashes']:
        raise ValueError('源码在测量后已更改，需重新测试')
    best_file = ROOT / 'records' / 'best.json'
    best = read_json(best_file) if best_file.exists() else {}
    parent = rec.get('parent')
    current = source_files(ROOT / args.problem)
    if parent:
        if best.get(args.problem) != parent:
            raise ValueError('当前最佳版本已变化或未建立基线，需基于最新版本重新比较')
        parent_rec = get_record(args.problem, parent)
        if current != parent_rec['source_hashes']:
            raise ValueError('题目录包含未登记改动，拒绝覆盖')
        verdict = comparison(parent_rec, rec)
        if not verdict['eligible']:
            raise ValueError('；'.join(verdict['reasons']))
    elif current != rec['source_hashes']:
        raise ValueError('首次建立基线必须与当前题目录完全相同')
    # Keep human-created files; only copy the measured source set.
    for name in rec['source_hashes']:
        target = ROOT / args.problem / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / 'source' / name, target)
    for name in current.keys() - rec['source_hashes'].keys():
        (ROOT / args.problem / name).unlink()
    best[args.problem] = args.version
    write_json(best_file, best)
    rec['promoted_at'] = now()
    write_json(record_path(args.problem, args.version), rec)
    print('已更新 %s 最佳版本为 %s；未进行正式平台提交' % (args.problem, args.version))


def report(args):
    lines = ['# 实验记录', '', '耗时合计仅用于本地筛选，不是官方得分；排行榜数据尚未录入。', '',
             '| 题目 | 版本 | 父版本 | 状态 | 合计 ms | 策略 |', '|---|---|---|---|---:|---|']
    hist = ROOT / 'records' / 'history.json'
    if hist.exists():
        for v in read_json(hist).get('versions', []):
            run = v.get('reference_run', {})
            if not isinstance(run, dict):
                run = next((r for r in v.get('runs', []) if r.get('id') == v.get('reference_run_id')), {})
            lines.append('| %s | %s | — | 历史证据 | %s | %s |' % (v['problem'], v['id'], run.get('total_time_ms', '—'), v.get('strategy', '').replace('|', '/')))
    for p in sorted((ROOT / 'records' / 'experiments').glob('*/*.json')):
        rec = read_json(p)
        lines.append('| %s | %s | %s | %s | %s | %s |' % (
            rec['problem'], rec['version'], rec.get('parent') or '—', rec['status'],
            ('%.2f' % rec['total_median_ms']) if 'total_median_ms' in rec else '—',
            rec['strategy'].replace('|', '/').replace('\n', ' ')))
    target = ROOT / 'records' / 'SUMMARY.md'
    target.write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('new', 'checkpoint', 'record', 'promote'):
        p = sub.add_parser(command)
        p.add_argument('problem', choices=CASES)
        p.add_argument('version')
        p.set_defaults(func=globals()[command])
        if command == 'new':
            p.add_argument('--parent')
            p.add_argument('--strategy', required=True)
        elif command == 'checkpoint':
            p.add_argument('--note', required=True, help='本地校验结果、待测事项和源码解释')
        elif command == 'record':
            p.add_argument('--log')
            p.add_argument('--environment', required=True)
            p.add_argument('--reference', required=True)
            p.add_argument('--repeats', type=int, default=3, help='独立完整套件轮数，不是TEST_RUNS')
            p.add_argument('--scheduler-status')
            p.add_argument('--failure', help='记录编译、排队、运行失败及放弃原因')
    p = sub.add_parser('compare')
    p.add_argument('problem', choices=CASES)
    p.add_argument('base')
    p.add_argument('candidate')
    p.set_defaults(func=compare)
    sub.add_parser('report').set_defaults(func=report)
    args = parser.parse_args()
    try:
        with locked():
            args.func(args)
    except (ValueError, OSError, KeyError) as exc:
        print('错误: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
