#!/usr/bin/env python3

import argparse
from datetime import datetime, timedelta
import glob
import multiprocessing
import os

from testbed.configuration import load_config
from testbed.network import setup, clean, setup_tc, clear_tc
from testbed.testbed import run_testcase

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
        start = datetime.now()
        delta = estimate_time(configs[i:])
        finish = (start+delta).time()
        print(
            f'{start.time()}: running testcase {i+1}/{len(configs)}: {config.name}. Estimated remaining time: {delta}, earliest finish time: {finish}')
        run_testcase(config, os.path.join(
            args.output, str(int(ts.timestamp())), config.name))
        end = datetime.now()
        print(f'finished testcase {config.name} after {end - start}')
        print()


def setup_cmd(args):
    setup()


def clean_cmd(args):
    clean()


def setup_tc_cmd(args):
    setup_tc(delay_us=10000, bandwidth='1mbit')


def clear_tc_cmd(args):
    clear_tc()


def main():
    multiprocessing.set_start_method('fork')

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
