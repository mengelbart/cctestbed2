#!/usr/bin/env python3
'''Measure the loss pattern of the bottleneck from a testcase's pcaps.

Every IPv4 packet entering the bottleneck router is matched against the packets
leaving it on the bottleneck interface. Unmatched packets were dropped by netem
or by the shaper and leaf qdiscs, so the shaper should not be a bottleneck when
validating a loss model. Results are reported per network step from tc.log and
compared with the configured loss model.
'''

import argparse
import json
import struct
import sys

from bisect import bisect_right
from collections import Counter
from datetime import datetime
from pathlib import Path

# Per direction: router ingress pcap, router egress pcap, sending subnet prefix
# and the router's own addresses, whose packets do not cross the bottleneck.
DIRECTIONS = {
    'forward': ('ns2-l12-b.pcap', 'ns2-l23-a.pcap', '10.0.12.', {'10.0.12.2', '10.0.23.1'}),
    'reverse': ('ns3-l34-a.pcap', 'ns3-l23-b.pcap', '10.0.34.', {'10.0.34.1', '10.0.23.2'}),
}

LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113
ETHERTYPE_IPV4 = 0x0800


def read_pcap(path: Path):
    '''Yields (timestamp, IPv4 packet bytes) from a classic pcap file.'''
    with open(path, 'rb') as f:
        header = f.read(24)
        if len(header) < 24:
            return
        magic = header[:4]
        if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
            endian = '<'
        elif magic in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d'):
            endian = '>'
        else:
            raise ValueError(f'{path}: not a classic pcap file')
        nano = magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d')
        linktype = struct.unpack(endian + 'I', header[20:24])[0] & 0x0fffffff
        if linktype == LINKTYPE_ETHERNET:
            l2_len, proto_at = 14, 12
        elif linktype == LINKTYPE_LINUX_SLL:
            l2_len, proto_at = 16, 14
        else:
            raise ValueError(f'{path}: unsupported link type {linktype}')
        record = struct.Struct(endian + 'IIII')
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            sec, frac, caplen, _ = record.unpack(rec)
            data = f.read(caplen)
            if len(data) < caplen:
                return
            if len(data) < l2_len + 20:
                continue
            if struct.unpack('!H', data[proto_at:proto_at + 2])[0] != ETHERTYPE_IPV4:
                continue
            yield sec + frac / (1e9 if nano else 1e6), data[l2_len:]


def addr(ip: bytes, offset: int) -> str:
    return '.'.join(str(b) for b in ip[offset:offset + 4])


def crosses_bottleneck(ip: bytes, prefix: str, router: set[str]) -> bool:
    return addr(ip, 12).startswith(prefix) and addr(ip, 16) not in router


def packet_key(ip: bytes) -> bytes:
    '''The IPv4 packet without TTL and header checksum, which forwarding changes.'''
    return ip[0:8] + ip[9:10] + ip[12:]


def loss_trace(ingress: Path, egress: Path, prefix: str, router: set[str]):
    '''Returns [(timestamp, lost)] for every packet entering the bottleneck, in order.'''
    delivered = Counter(packet_key(ip) for _, ip in read_pcap(egress)
                        if crosses_bottleneck(ip, prefix, router))
    trace = []
    for ts, ip in read_pcap(ingress):
        if not crosses_bottleneck(ip, prefix, router):
            continue
        key = packet_key(ip)
        if delivered[key] > 0:
            delivered[key] -= 1
            trace.append((ts, False))
        else:
            trace.append((ts, True))
    return trace


def runs(lost: list[bool]):
    '''Returns lengths of loss bursts and of the delivery gaps between them.'''
    bursts, gaps = [], []
    length = 0
    for i, value in enumerate(lost):
        length += 1
        if i + 1 == len(lost) or lost[i + 1] != value:
            (bursts if value else gaps).append(length)
            length = 0
    return bursts, gaps


def stationary(matrix: list[list[float]], iterations: int = 10000) -> list[float]:
    n = len(matrix)
    pi = [1 / n] * n
    for _ in range(iterations):
        pi = [sum(pi[i] * matrix[i][j] for i in range(n)) for j in range(n)]
    return pi


def expected(loss) -> dict:
    '''Expected loss rate, and burst and gap means for simple Gilbert, as fractions.'''
    if not isinstance(loss, dict):
        return {'loss': loss / 100}
    v = {k: x / 100 for k, x in loss.items() if k != 'model'}
    if loss['model'] == 'random':
        return {'loss': v['percent']}
    if loss['model'] == 'gemodel':
        p, r = v['p'], v['r']
        bad, good = v.get('loss_bad', 1.0), v.get('loss_good', 0.0)
        if p + r == 0:
            return {'loss': good}
        pi_bad = p / (p + r)
        result = {'loss': (1 - pi_bad) * good + pi_bad * bad}
        if bad == 1 and good == 0:
            result['burst'] = 1 / r if r else float('inf')
            result['gap'] = 1 / p if p else float('inf')
            result['r'] = r
        return result
    # 4-state model, states 1 to 4 as indices 0 to 3, 3 and 4 lose the packet
    p13, p31 = v['p13'], v['p31']
    p32, p23, p14 = v.get('p32', 0.0), v.get('p23', 1.0), v.get('p14', 0.0)
    matrix = [
        [1 - p13 - p14, 0, p13, p14],
        [0, 1 - p23, p23, 0],
        [p31, p32, 1 - p31 - p32, 0],
        [1, 0, 0, 0],
    ]
    pi = stationary(matrix)
    return {'loss': pi[2] + pi[3]}


def load_steps(out: Path) -> list[tuple[float, dict]]:
    '''(start time, network config) per step. The last tc.log line marks the end.'''
    steps = []
    with open(out / 'tc.log') as f:
        for line in f:
            config = json.loads(line)
            steps.append((datetime.fromisoformat(config['time']).timestamp(), config))
    return steps


def fmt(x: float | None, percent: bool = False) -> str:
    if x is None:
        return '-'
    return f'{100 * x:.2f}%' if percent else f'{x:.2f}'


def report(name: str, lost: list[bool], loss, max_hist: int):
    model = loss.get('model') if isinstance(loss, dict) else 'random'
    print(f'{name}: loss {json.dumps(loss)}')
    if not lost:
        print('  no packets')
        return
    exp = expected(loss)
    bursts, gaps = runs(lost)
    # gaps before the first and after the last burst are cut by the trace
    inner_gaps = gaps[(not lost[0]):len(gaps) - (not lost[-1])]
    mean_burst = sum(bursts) / len(bursts) if bursts else None
    mean_gap = sum(inner_gaps) / len(inner_gaps) if inner_gaps else None
    n_lost = sum(lost)
    print(f'  packets {len(lost)}, lost {n_lost}')
    print(f'  {"":12}{"measured":>12}{"expected":>12}')
    print(f'  {"loss rate":12}{fmt(n_lost / len(lost), True):>12}{fmt(exp["loss"], True):>12}')
    print(f'  {"mean burst":12}{fmt(mean_burst):>12}{fmt(exp.get("burst")):>12}')
    print(f'  {"mean gap":12}{fmt(mean_gap):>12}{fmt(exp.get("gap")):>12}')
    if model in ('gemodel', 'random') and mean_burst and mean_gap:
        print(f'  Gilbert fit: p {fmt(1 / mean_gap, True)}, r {fmt(1 / mean_burst, True)}')
    if bursts:
        hist = Counter(min(b, max_hist) for b in bursts)
        print(f'  burst length histogram ({len(bursts)} bursts, last bin is >= {max_hist}):')
        r = exp.get('r')
        for length in range(1, max_hist + 1):
            line = f'    {length:>3}{hist[length]:>9}'
            if r is not None:
                # burst lengths are geometric with success probability r
                prob = (1 - r) ** (length - 1) * (r if length < max_hist else 1)
                line += f'{len(bursts) * prob:>11.1f} expected'
            print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('output', type=Path, help='testcase output directory')
    parser.add_argument('-d', '--direction', choices=DIRECTIONS, default='forward',
                        help='forward is ns1 -> ns4, reverse is ns4 -> ns1')
    parser.add_argument('--max-burst', type=int, default=10,
                        help='last bin of the burst length histogram')
    args = parser.parse_args()

    ingress, egress, prefix, router = DIRECTIONS[args.direction]
    for pcap in (ingress, egress):
        if not (args.output / pcap).exists():
            sys.exit(f'{args.output / pcap}: not found')
    trace = loss_trace(args.output / ingress, args.output / egress, prefix, router)
    steps = load_steps(args.output)
    if len(steps) < 2:
        sys.exit(f'{args.output / "tc.log"}: no complete network step')
    starts = [t for t, _ in steps]
    per_step = [[] for _ in steps[:-1]]
    for ts, lost in trace:
        i = bisect_right(starts, ts) - 1
        if 0 <= i < len(per_step):
            per_step[i].append(lost)
    for i, (lost, (_, config)) in enumerate(zip(per_step, steps)):
        loss = config['loss'] if config['traffic_control'] else 0
        report(f'network[{i}]', lost, loss, args.max_burst)


if __name__ == '__main__':
    main()
