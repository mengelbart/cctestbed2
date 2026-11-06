#!/usr/bin/env python3

import argparse
from datetime import datetime, timedelta
import glob
import multiprocessing
import os
import signal
import sys

from pathlib import Path

from testbed.configuration import SHAPERS, ConfigError, load_config
from testbed.network import MAX_FRAME, setup, clean, setup_tc, clear_tc
from testbed.testbed import run_testcase

def estimate_time(configs):
    return timedelta(seconds=sum([config.duration for config in configs]))


def run_cmd(args):
    if args.glob:
        testcases = glob.glob(args.glob)
    elif args.testcases:
        testcases = args.testcases
    else:
        raise RuntimeError('no testcase configs given')

    ts = datetime.now()
    configs = []
    errors = []
    for testcase in testcases:
        try:
            configs.append(load_config(testcase))
        except ConfigError as e:
            errors.append(str(e))
    by_name = {}
    for testcase in testcases:
        by_name.setdefault(Path(testcase).stem, []).append(testcase)
    for name, files in by_name.items():
        if len(files) > 1:
            errors.append(f'{", ".join(files)}: same name {name!r}, outputs would overwrite each other')
    if errors:
        print('invalid testcase configs, nothing was run:', file=sys.stderr)
        print('\n'.join(errors), file=sys.stderr)
        sys.exit(2)
    print(f'running testcases: {testcases}')
    failed = []
    for i, config in enumerate(configs):
        start = datetime.now()
        delta = estimate_time(configs[i:])
        finish = (start+delta).time()
        print(
            f'{start.time()}: running testcase {i+1}/{len(configs)}: {config.name}. Estimated remaining time: {delta}, earliest finish time: {finish}')
        output_dir = os.path.join(
            args.output, str(int(ts.timestamp())), config.name)
        if not run_testcase(config, output_dir):
            failed.append(output_dir)
        end = datetime.now()
        print(f'finished testcase {config.name} after {end - start}')
        print()

    if failed:
        print(f'{len(failed)}/{len(configs)} testcases FAILED, see FAILED file in:')
        for output_dir in failed:
            print(f'  {output_dir}')
        sys.exit(1)


def setup_cmd(args):
    setup()


def clean_cmd(args):
    clean()


def setup_tc_cmd(args):
    setup_tc(delay_us=args.delay, bandwidth=args.bandwidth, limit=args.limit,
             burst=args.burst, shaper=args.shaper, aqm=args.aqm,
             aqm_options=args.aqm_options, loss=args.loss)


def clear_tc_cmd(args):
    clear_tc()


def main():
    multiprocessing.set_start_method('fork')
    # raise SystemExit on SIGTERM so testcase cleanup runs
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

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

    setup_tc = subparsers.add_parser(
        'tc', help='add delay and rate limit qdiscs on the bottleneck',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    setup_tc.add_argument('--delay', type=int, default=10000,
                          help='one-way delay in microseconds')
    setup_tc.add_argument('--bandwidth', default='1mbit',
                          help='tc rate string')
    setup_tc.add_argument('--limit', type=int, default=12500,
                          help='bottleneck buffer in bytes')
    setup_tc.add_argument('--burst', type=int, default=6250,
                          help='shaper burst in bytes')
    setup_tc.add_argument('--shaper', choices=SHAPERS, default='tbf',
                          help='rate limiting qdisc')
    setup_tc.add_argument('--aqm', help='leaf qdisc, e.g. dualpi2, requires --shaper htb')
    setup_tc.add_argument('--aqm-options', help='extra tc arguments for --aqm')
    setup_tc.add_argument('--loss', type=float, default=0,
                          help='random loss in percent')
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
    if args.func is setup_tc_cmd:
        if args.aqm is not None and args.shaper != 'htb':
            parser.error('--aqm requires --shaper htb')
        if args.limit < MAX_FRAME or args.burst < MAX_FRAME:
            parser.error(f'--limit and --burst must be at least {MAX_FRAME} bytes')
    args.func(args)


if __name__ == "__main__":
    main()
