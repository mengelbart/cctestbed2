#!/usr/bin/env python3

import argparse
from datetime import datetime, timedelta
import glob
from subprocess import TimeoutExpired
import threading
import time
import os

from pathlib import Path
from typing import List

from configuration import ApplicationConfig, EnvVariable, NetworkConfig, Testcase, load_config
from network import add_delay, remove_bandwidth_limit, remove_delay, set_bandwidth_limit, setup, clean, setup_tc, clear_tc, start


def env_var_to_dict(env_vars: List[EnvVariable]) -> dict:
    res = {env_var.name: env_var.value for env_var in env_vars}
    # rewrite paths in environment variables to be absolute so that they can 
    # be found during the experiment which runs in a different CWD.
    for name, value in res.items():
        if os.path.exists(value) or os.path.isdir(value):
            res[name] = os.path.abspath(value)
    return res


def traffic_controller(configs: list[NetworkConfig]):
    for i, config in enumerate(configs):
        verb = 'add' if i == 0 else 'change'
        print(f'changing network config: {verb} - {config}')
        if config.traffic_control:
            add_delay(config.delay, verb=verb)
            set_bandwidth_limit(
                rate=config.bandwidth, latency=config.latency, verb=verb)
        else:
            remove_delay()
            remove_bandwidth_limit()
        time.sleep(config.duration)


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


def run_testcase(testcase: Testcase, output_dir: str):
    setup()
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    tcpdump_ps = []
    for i in [1, 2, 3, 4]:
        tcpdump_ps.append(start(f'ns{i}', 'tcpdump', [
            '-s', '200', '-w', f'ns{i}.pcap'], cwd=output_dir))

    traffic_controller_thread = threading.Thread(
        target=traffic_controller, kwargs={'configs': testcase.network})
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
            print('tp terminated')
        except TimeoutError as e:
            tp.kill()
            print(f'tp killed: {e}')

    clean()


def estimate_time(configs):
    return timedelta(seconds=sum([config.duration for config in configs]))


def run_cmd(args):
    print(args)
    if args.glob:
        testcases = glob.glob(args.glob)
    elif args.testcases:
        testcases = args.testcases
    else:
        raise RuntimeError('no testcase configs given')

    ts = datetime.now()
    configs = [load_config(testcase) for testcase in testcases]
    print(f'running testcases: {testcases}')
    for i, config in enumerate(configs):
        now = datetime.now()
        delta = estimate_time(configs[i:])
        finish = (now+delta).time()
        print(
            f'{now.time()}: running testcase {i+1}/{len(configs)}: {config.name}. Estimated remaining time: {delta}, earliest finish time: {finish}')
        run_testcase(config, os.path.join(args.output, str(int(ts.timestamp())), config.name))
        print(f'finished testcase: {config.name}')
        print()


def setup_cmd(args):
    setup()


def clean_cmd(args):
    clean()


def setup_tc_cmd(args):
    setup_tc(delay_us=100000, bandwidth='1mbit')


def clear_tc_cmd(args):
    clear_tc()


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(help='sub-command help', required=True)

    parser.add_argument('-o', '--output', default='out/',
                        help='Base output directory')

    clean = subparsers.add_parser(
        'clean', help='clean up virtual interaces and namespaces')
    clean.set_defaults(func=clean_cmd)

    setup = subparsers.add_parser(
        'setup', help='setup virtual interfaces and namespaces')
    setup.set_defaults(func=setup_cmd)

    setup_tc = subparsers.add_parser('tc', help='add netem delay qdisc')
    setup_tc.set_defaults(func=setup_tc_cmd)

    clean_tc = subparsers.add_parser('clear', help='remove any tc qdiscs')
    clean_tc.set_defaults(func=clear_tc_cmd)

    run = subparsers.add_parser('run', help='run one or more testcases')
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument('-t', '--testcases', nargs='+',
                       help='one or more testcase config files')
    group.add_argument('-g', '--glob', help='glob for selecting config files')
    run.set_defaults(func=run_cmd)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
