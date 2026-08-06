import json
import time
import threading
import os

from pathlib import Path
from dataclasses import asdict
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo
from subprocess import TimeoutExpired

from .configuration import Testcase, NetworkConfig, ApplicationConfig, EnvVariable
from .network import set_delay, remove_bandwidth_limit, remove_delay, set_bandwidth_limit, setup, clean, setup_tc, clear_tc, start


def get_time():
    return datetime.now(ZoneInfo("Europe/Berlin")).isoformat()


def env_var_to_dict(env_vars: List[EnvVariable]) -> dict:
    return {env_var.name: env_var.value for env_var in env_vars}


def traffic_controller(output_dir: str, configs: list[NetworkConfig]):
    log_file = Path(output_dir) / Path('tc.log')
    with open(log_file, 'w') as log:
        if len(configs) == 0:
            return
        last_config = None
        for i, config in enumerate(configs):
            last_config = config
            verb = 'add' if i == 0 else 'change'
            ts = get_time()
            data = asdict(config)
            data['time'] = ts
            json.dump(data, log)
            log.write('\n')
            print(f'{ts} changing network config: {verb} - {config}')
            if config.traffic_control:
                set_delay(config.delay, verb=verb)
                set_bandwidth_limit(
                    rate=config.bandwidth, limit=config.limit,
                    burst=config.burst, verb=verb)
            else:
                remove_delay()
                remove_bandwidth_limit()
            time.sleep(config.duration)
        ts = get_time()
        data = asdict(last_config)
        data['time'] = ts
        json.dump(data, log)
        log.write('\n')


def app_runner(output_dir: str, application: ApplicationConfig):
    env_vars = env_var_to_dict(application.environment)
    env = os.environ.copy()
    env.update(env_vars)
    stdout = Path(output_dir) / Path(f'{application.name}.stdout.log')
    stderr = Path(output_dir) / Path(f'{application.name}.stderr.log')

    with open(stdout, 'w') as sout, open(stderr, 'w') as serr:
        time.sleep(application.start_time)
        print(f'{application.binary} {' '.join(application.arguments)}')
        p = start(application.namespace, application.binary, application.arguments,
                  cwd=output_dir, env=env, stdout=sout, stderr=serr)
        time.sleep(application.duration)
        try:
            p.terminate()
            p.wait(1)
        except TimeoutExpired as e:
            print(
                f'timeout while waiting for processes to exit, kiling client and server ({e})')
            p.kill()
        finally:
            p.release()


def run_testcase(testcase: Testcase, output_dir: str):
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

    traffic_controller_thread = threading.Thread(
        target=traffic_controller, kwargs={'output_dir': output_dir, 'configs': testcase.network})
    traffic_controller_thread.start()

    app_controller_threads = []
    for application in testcase.applications:
        app_thread = threading.Thread(
            target=app_runner, kwargs={
                'output_dir': output_dir, 'application': application}
        )
        app_thread.start()
        app_controller_threads.append(app_thread)

    for apt in app_controller_threads:
        apt.join()
    print('apps joined')

    traffic_controller_thread.join()
    print('tc joined')

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


