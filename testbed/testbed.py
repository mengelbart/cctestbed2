import json
import os
import threading
import traceback

from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

from pathlib import Path
from dataclasses import asdict
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo
from subprocess import TimeoutExpired

from .configuration import Testcase, NetworkConfig, ApplicationConfig, EnvVariable
from .network import set_delay, remove_bandwidth_limit, set_bandwidth_limit, set_leaf, remove_leaf, remove_tc, setup, clean, setup_tc, clear_tc, start


def get_time():
    return datetime.now(ZoneInfo("Europe/Berlin")).isoformat()


def env_var_to_dict(env_vars: List[EnvVariable]) -> dict:
    return {env_var.name: env_var.value for env_var in env_vars}


def traffic_controller(output_dir: str, configs: list[NetworkConfig],
                       stop: threading.Event):
    log_file = Path(output_dir) / Path('tc.log')
    with open(log_file, 'w') as log:
        if len(configs) == 0:
            return
        prev_config = None
        for config in configs:
            prev_on = prev_config is not None and prev_config.traffic_control
            shaper_changed = prev_on and prev_config.shaper != config.shaper
            leaf_changed = prev_on and not shaper_changed and prev_config.aqm != config.aqm
            delay_verb = 'change' if prev_on else 'add'
            shaper_verb = 'change' if prev_on and not shaper_changed else 'add'
            ts = get_time()
            data = asdict(config)
            data['time'] = ts
            json.dump(data, log)
            log.write('\n')
            print(f'{ts} changing network config: delay {delay_verb}, shaper {shaper_verb} - {config}')
            if config.traffic_control:
                if shaper_changed:
                    remove_bandwidth_limit()
                if leaf_changed:
                    remove_leaf(config.shaper)
                set_delay(config.delay, verb=delay_verb, limit=config.netem_limit)
                set_bandwidth_limit(
                    rate=config.bandwidth, limit=config.limit,
                    burst=config.burst, verb=shaper_verb, shaper=config.shaper)
                set_leaf(config.aqm, config.aqm_options, limit=config.limit,
                         shaper=config.shaper)
            else:
                remove_tc()
            prev_config = config
            if stop.wait(config.duration):
                break
        ts = get_time()
        data = asdict(prev_config)
        data['time'] = ts
        json.dump(data, log)
        log.write('\n')


def app_runner(output_dir: str, application: ApplicationConfig,
               stop: threading.Event):
    env_vars = env_var_to_dict(application.environment)
    env = os.environ.copy()
    env.update(env_vars)
    stdout = Path(output_dir) / Path(f'{application.name}.stdout.log')
    stderr = Path(output_dir) / Path(f'{application.name}.stderr.log')

    with open(stdout, 'w') as sout, open(stderr, 'w') as serr:
        if stop.wait(application.start_time):
            return
        print(f'{application.binary} {' '.join(application.arguments)}')
        p = start(application.namespace, application.binary, application.arguments,
                  cwd=output_dir, env=env, stdout=sout, stderr=serr)
        stop.wait(application.duration)
        try:
            p.terminate()
            p.wait(1)
        except TimeoutExpired as e:
            print(
                f'timeout while waiting for processes to exit, kiling client and server ({e})')
            p.kill()
        finally:
            p.release()


def run_testcase(testcase: Testcase, output_dir: str) -> bool:
    '''Run one testcase. Returns False and writes `FAILED` to output_dir if a worker raised.'''
    setup()
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cf = Path(output_dir) / Path('config.json')
    with open(cf, 'w') as c:
        data = asdict(testcase)
        data['time'] = get_time()
        json.dump(data, c)

    tcpdump_ps = []
    for i in [1, 2, 3, 4]:
        tcpdump_ps.append(start(f'ns{i}', 'tcpdump', [
            '-s', '200', '-w', f'ns{i}.pcap'], cwd=output_dir))

    # set when any worker fails so the others end early
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1 + len(testcase.applications)) as pool:
        futures = {pool.submit(traffic_controller, output_dir, testcase.network, stop):
                   'traffic controller'}
        for application in testcase.applications:
            futures[pool.submit(app_runner, output_dir, application, stop)] = \
                f'application {application.name}'
        done, _ = wait(futures, return_when=FIRST_EXCEPTION)
        if any(f.exception() is not None for f in done):
            print('worker failed, stopping testcase early')
            stop.set()
    print('workers joined')

    errors = []
    for future, worker in futures.items():
        e = future.exception()
        if e is not None:
            msg = ''.join(traceback.format_exception(e))
            print(f'ERROR in {worker}:\n{msg}')
            errors.append(f'{worker}:\n{msg}')
    if errors:
        with open(Path(output_dir) / 'FAILED', 'w') as f:
            f.write('\n'.join(errors))

    for tp in tcpdump_ps:
        try:
            tp.terminate()
            tp.wait(1)
            print('tcpdump process terminated')
        except TimeoutError as e:
            tp.kill()
            print(f'tcpdump process killed: {e}')
        finally:
            tp.release()

    clean()

    dir = Path(output_dir)
    dir.chmod(0o777)
    for path in dir.rglob('*'):
        try:
            path.chmod(0o777)
        except Exception as e:
            print('WARNING: failed to chmod {path} in output directory: {e}')

    return len(errors) == 0


