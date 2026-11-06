import json
import os
import threading
import time
import traceback

from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

from pathlib import Path
from dataclasses import asdict
from datetime import datetime
from typing import List
from subprocess import TimeoutExpired

from .configuration import Testcase, NetworkConfig, ApplicationConfig, EnvVariable
from .network import set_delay, remove_bandwidth_limit, set_bandwidth_limit, set_leaf, remove_leaf, remove_tc, capture_points, setup, clean, start, bottleneck_handles


def get_time():
    return datetime.now().astimezone().isoformat()


def env_var_to_dict(env_vars: List[EnvVariable]) -> dict:
    return {env_var.name: env_var.value for env_var in env_vars}


def wait_until(stop: threading.Event, target: float) -> bool:
    '''Wait until time.monotonic() reaches target. Returns True if stop was set.'''
    return stop.wait(max(0, target - time.monotonic()))


def log_config(log, config: NetworkConfig, t0: float):
    data = asdict(config)
    data['time'] = get_time()
    data['offset'] = time.monotonic() - t0
    json.dump(data, log)
    log.write('\n')


def traffic_controller(output_dir: str, configs: list[NetworkConfig], handles: dict,
                       t0: float, stop: threading.Event):
    log_file = Path(output_dir) / Path('tc.log')
    with open(log_file, 'w') as log:
        if len(configs) == 0:
            return
        prev_config = None
        target = t0
        for config in configs:
            prev_on = prev_config is not None and prev_config.traffic_control
            shaper_changed = prev_on and prev_config.shaper != config.shaper
            leaf_changed = prev_on and not shaper_changed and prev_config.aqm != config.aqm
            delay_verb = 'change' if prev_on else 'add'
            shaper_verb = 'change' if prev_on and not shaper_changed else 'add'
            print(f'{get_time()} changing network config: delay {delay_verb}, shaper {shaper_verb} - {config}')
            if config.traffic_control:
                if shaper_changed:
                    remove_bandwidth_limit(handles)
                if leaf_changed:
                    remove_leaf(config.shaper)
                set_delay(handles, config.delay, verb=delay_verb, limit=config.netem_limit)
                set_bandwidth_limit(
                    handles, rate=config.bandwidth, limit=config.limit,
                    burst=config.burst, verb=shaper_verb, shaper=config.shaper)
                set_leaf(config.aqm, config.aqm_options, limit=config.limit,
                         shaper=config.shaper)
            else:
                remove_tc(handles)
            log_config(log, config, t0)
            prev_config = config
            target += config.duration
            if wait_until(stop, target):
                break
        log_config(log, prev_config, t0)


def app_runner(output_dir: str, application: ApplicationConfig,
               t0: float, stop: threading.Event):
    env_vars = env_var_to_dict(application.environment)
    env = os.environ.copy()
    env.update(env_vars)
    stdout = Path(output_dir) / Path(f'{application.name}.stdout.log')
    stderr = Path(output_dir) / Path(f'{application.name}.stderr.log')

    with open(stdout, 'w') as sout, open(stderr, 'w') as serr:
        start_at = t0 + application.start_time
        if wait_until(stop, start_at):
            return
        print(f'{application.binary} {' '.join(application.arguments)}')
        p = start(application.namespace, application.binary, application.arguments,
                  cwd=output_dir, env=env, stdout=sout, stderr=serr)
        wait_until(stop, start_at + application.duration)
        try:
            p.terminate()
            p.wait(1)
        except TimeoutExpired as e:
            print(
                f'timeout while waiting for processes to exit, kiling client and server ({e})')
            p.kill()
            p.wait()


def wait_for_files(paths: list[Path], timeout: float = 5):
    '''Wait until all files exist. tcpdump creates its file once capturing is active.'''
    deadline = time.monotonic() + timeout
    while not all(p.exists() for p in paths):
        if time.monotonic() > deadline:
            missing = [str(p) for p in paths if not p.exists()]
            raise RuntimeError(f'tcpdump did not start within {timeout}s: {missing}')
        time.sleep(0.05)


# libpcap hands packets to tcpdump in blocks with a ~1 s timeout, and pending
# blocks are lost on SIGTERM.
TCPDUMP_FLUSH_DELAY = 1.5


def stop_tcpdumps(procs, timeout: float = 5):
    time.sleep(TCPDUMP_FLUSH_DELAY)
    for p in procs:
        try:
            p.terminate()
        except Exception as e:
            print(f'WARNING: failed to terminate tcpdump: {e}')
    for p in procs:
        try:
            p.wait(timeout)
        except TimeoutExpired as e:
            print(f'WARNING: killing tcpdump, pcap may be truncated ({e})')
            p.kill()
            p.wait()
        except Exception as e:
            print(f'WARNING: failed to stop tcpdump: {e}')
    print('tcpdump processes stopped')


def format_error(e: BaseException) -> str:
    return ''.join(traceback.format_exception(e))


def run_workers(testcase: Testcase, output_dir: str) -> list[str]:
    '''Run traffic controller and applications. Returns error descriptions of failed workers.'''
    # set when any worker fails or on interrupt so the others end early
    stop = threading.Event()
    futures = {}
    # opened before any worker thread exists, as opening a NetNS forks
    with bottleneck_handles() as handles:
        pool = ThreadPoolExecutor(max_workers=1 + len(testcase.applications))
        t0 = time.monotonic()
        try:
            futures[pool.submit(traffic_controller, output_dir, testcase.network, handles, t0, stop)] = \
                'traffic controller'
            for application in testcase.applications:
                futures[pool.submit(app_runner, output_dir, application, t0, stop)] = \
                    f'application {application.name}'
            # stop is set on return, which ends the testcase at t0 + duration
            timeout = max(0, t0 + testcase.duration - time.monotonic())
            done, _ = wait(futures, timeout=timeout, return_when=FIRST_EXCEPTION)
            if any(f.exception() is not None for f in done):
                print('worker failed, stopping testcase early')
        finally:
            stop.set()
            pool.shutdown(wait=True)
    print('workers joined')

    return [f'{worker}:\n{format_error(f.exception())}'
            for f, worker in futures.items() if f.exception() is not None]


def fix_ownership(paths: list[Path]):
    '''Give paths to the user who invoked sudo, or make them readable for everyone otherwise.'''
    uid, gid = os.environ.get('SUDO_UID'), os.environ.get('SUDO_GID')
    for path in paths:
        try:
            if uid is not None and gid is not None:
                os.chown(path, int(uid), int(gid), follow_symlinks=False)
            elif not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        except Exception as e:
            print(f'WARNING: failed to fix ownership of {path}: {e}')


def run_testcase(testcase: Testcase, output_dir: str) -> bool:
    '''Run one testcase. Returns False and writes `FAILED` to output_dir on any error.

    KeyboardInterrupt and SystemExit are recorded the same way and re-raised after cleanup.
    '''
    out = Path(output_dir)
    created = [d for d in [out, *out.parents] if not d.exists()]
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    tcpdump_ps = []
    try:
        setup()

        with open(out / 'config.json', 'w') as c:
            data = asdict(testcase)
            data['time'] = get_time()
            json.dump(data, c)

        pcaps = []
        for ns, ifname, pcap in capture_points():
            tcpdump_ps.append(start(ns, 'tcpdump', [
                '-i', ifname, '-s', '200', '-w', pcap], cwd=output_dir))
            pcaps.append(out / pcap)
        wait_for_files(pcaps)

        errors += run_workers(testcase, output_dir)
    except BaseException as e:
        errors.append(f'testcase:\n{format_error(e)}')
        if not isinstance(e, Exception):
            raise
    finally:
        try:
            stop_tcpdumps(tcpdump_ps)
        except Exception as e:
            print(f'WARNING: stopping tcpdump failed: {e}')
        try:
            clean()
        except Exception as e:
            print(f'WARNING: removing namespaces failed: {e}')

        if errors:
            for error in errors:
                print(f'ERROR in {error}')
            with open(out / 'FAILED', 'w') as f:
                f.write('\n'.join(errors))

        fix_ownership(created + list(out.rglob('*')))

    return not errors
