
import subprocess
import os
import signal

from pathlib import Path

from pyroute2 import netns, IPRoute, NSPopen, NetNS
from pyroute2.netlink.exceptions import NetlinkError

NAMESPACES = [
    {
        'name': 'ns1',
        'routes': [
            {
                'dst': '10.2.0.0/24',
                'gateway': '10.1.0.20',
                'if': 'v1p1',
            },
            {
                'dst': '10.3.0.0/24',
                'gateway': '10.1.0.20',
                'if': 'v1p1',
            },
        ],
    },
    {
        'name': 'ns2',
        'routes': [
            {
                'dst': '10.3.0.0/24',
                'gateway': '10.2.0.20',
                'if': 'v3p1',
            },
        ],
    },
    {
        'name': 'ns3',
        'routes': [
            {
                'dst': '10.1.0.0/24',
                'gateway': '10.2.0.10',
                'if': 'v4p1',
            },
        ],
    },
    {
        'name': 'ns4',
        'routes': [
            {
                'dst': '10.1.0.0/24',
                'gateway': '10.3.0.10',
                'if': 'v6p1',
            },
            {
                'dst': '10.2.0.0/24',
                'gateway': '10.3.0.10',
                'if': 'v6p1',
            },
        ],
    },
]

BRIDGES = [
    {
        'name': 'br1',
        'address': '10.1.0.1',
        'mask': 24,
    },
    {
        'name': 'br2',
        'address': '10.2.0.1',
        'mask': 24,
    },
    {
        'name': 'br3',
        'address': '10.3.0.1',
        'mask': 24,
    },
]

DEVICES = [
    {
        'name': 'v1p1',
        'peer': 'v1p2',
        'ns': 'ns1',
        'ip': '10.1.0.10',
        'mask': 24,
        'broadcast': '10.1.0.255',
        'bridge': 'br1',
    },
    {
        'name': 'v2p1',
        'peer': 'v2p2',
        'ns': 'ns2',
        'ip': '10.1.0.20',
        'mask': 24,
        'broadcast': '10.1.0.255',
        'bridge': 'br1',
    },
    {
        'name': 'v3p1',
        'peer': 'v3p2',
        'ns': 'ns2',
        'ip': '10.2.0.10',
        'mask': 24,
        'broadcast': '10.2.0.255',
        'bridge': 'br2',
    },
    {
        'name': 'v4p1',
        'peer': 'v4p2',
        'ns': 'ns3',
        'ip': '10.2.0.20',
        'mask': 24,
        'broadcast': '10.2.0.255',
        'bridge': 'br2',
    },
    {
        'name': 'v5p1',
        'peer': 'v5p2',
        'ns': 'ns3',
        'ip': '10.3.0.10',
        'mask': 24,
        'broadcast': '10.3.0.255',
        'bridge': 'br3',
    },
    {
        'name': 'v6p1',
        'peer': 'v6p2',
        'ns': 'ns4',
        'ip': '10.3.0.20',
        'mask': 24,
        'broadcast': '10.3.0.255',
        'bridge': 'br3',
    },
]


def create_ns():
    for ns in NAMESPACES:
        try:
            netns.create(ns['name'])
        except Exception as e:
            print(e)


def remove_ns():
    for ns in NAMESPACES:
        try:
            netns.remove(ns['name'])
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f'{e}:', type(e).__name__)


def create_bridge():
    with IPRoute() as ipr:
        for br in BRIDGES:
            try:
                ipr.link('add', ifname=br['name'], kind='bridge')
                dev = ipr.link_lookup(ifname=br['name'])[0]
                ipr.link('set', index=dev, state='up')
                ipr.addr('add', index=dev, address=br['address'],
                         mask=br['mask'])
            except Exception as e:
                print(f'error: {e}, while creating bridge: {br}')


def remove_bridge():
    with IPRoute() as ipr:
        for br in BRIDGES:
            try:
                ipr.link('del', ifname=br['name'])
            except NetlinkError as e:
                if e.code == 19:  # No such device
                    continue
                print(f'{e}:', type(e).__name__)


def create_iface():
    with IPRoute() as ipr:
        for config in DEVICES:
            try:
                ipr.link('add', ifname=config['name'], kind='veth',
                         peer=config['peer'])
                peer = ipr.link_lookup(ifname=config['peer'])[0]
                br = ipr.link_lookup(ifname=config['bridge'])[0]
                ipr.link('set', index=peer, master=br)
                dev = ipr.link_lookup(ifname=config['name'])[0]
                ipr.link('set', index=dev, net_ns_fd=config['ns'])
                ns = NetNS(config['ns'])
                ns.addr('add', index=dev, address=config['ip'],
                        mask=config['mask'], broadcast=config['broadcast'])
                ns.link('set', index=dev, state='up')
                lo = ns.link_lookup(ifname='lo')[0]
                ns.link('set', index=lo, state='up')
                ns.close()
                ipr.link('set', index=peer, state='up')
            except Exception as e:
                print(f'{type(e).__name__}: {e}, config: {config}')


def remove_iface():
    with IPRoute() as ipr:
        for config in DEVICES:
            try:
                devs = ipr.link_lookup(ifname=config['name'])
                peers = ipr.link_lookup(ifname=config['peer'])
                if len(devs) > 0:
                    ipr.link('del', index=devs[0])
                if len(peers) > 0:
                    ipr.link('del', index=peers[0])
            except Exception as e:
                print(f'{e}: ', type(e).__name__)


def create_routes():
    for namespace in NAMESPACES:
        for route in namespace['routes']:
            try:
                ns = NetNS(namespace['name'])
                # iface = ns.link_lookup(ifname=route['if'])[0]
                ns.route('add', dst=route['dst'], gateway=route['gateway'])
                ns.close()
            except Exception as e:
                print(e, namespace, route)


def add_delay(delay_us=10000, verb='add'):
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='v3p1')[0]
        ns.tc(verb, 'netem', index=dev, handle='1:', delay=delay_us)
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='v4p1')[0]
        ns.tc(verb, 'netem', index=dev, handle='1:', delay=delay_us)


def remove_delay():
    if len(netns.listnetns()) == 0:
        return
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='v3p1')[0]
        ns.tc('del', index=dev, handle='1:')
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='v4p1')[0]
        ns.tc('del', index=dev, handle='1:')


def set_bandwidth_limit(rate='10mbit', latency='50ms', burst=10000, verb='add'):
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='v3p1')[0]
        ns.tc(verb, 'tbf', index=dev, handle='0:', parent='1:', rate=rate,
              latency=latency, burst=burst)
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='v4p1')[0]
        ns.tc(verb, 'tbf', index=dev, handle='0:', parent='1:', rate=rate,
              latency=latency, burst=burst)


def remove_bandwidth_limit():
    if len(netns.listnetns()) == 0:
        return
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='v3p1')[0]
        ns.tc('del', index=dev, handle='0:', parent='1:')
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='v4p1')[0]
        ns.tc('del', index=dev, handle='0:', parent='1:')


def setup_tc(delay_us=0, bandwidth='1mbit', verb='add'):
    add_delay(delay_us)
    set_bandwidth_limit(bandwidth)


def clear_tc():
    try:
        remove_bandwidth_limit()
    except Exception as e:
        print(e)
    try:
        remove_delay()
    except Exception as e:
        print(e)


def ping_all():
    procs = []
    for ns in NAMESPACES:
        for device in DEVICES:
            procs.append(start(ns['name'], 'ping', [
                '-c', '1', '-4', device['ip']], stdout=subprocess.PIPE))
        for brdige in BRIDGES:
            procs.append(start(ns['name'], 'ping', [
                         '-c', '1', '-4', brdige['address']], stdout=subprocess.PIPE))
    for proc in procs:
        ret = proc.wait()
        proc.release()
        if ret != 0:
            print(
                f'WARNING: ping command exited with non-zero exit code: {ret} ({proc.args})')


def setup_kernel():
    subprocess.run(['modprobe', 'br_netfilter'], check=True)
    subprocess.run(['modprobe', 'sch_netem'], check=True)
    subprocess.run(
        ['sysctl', '-w', 'net.bridge.bridge-nf-call-arptables=0'], check=True)
    subprocess.run(
        ['sysctl', '-w', 'net.bridge.bridge-nf-call-ip6tables=0'], check=True)
    subprocess.run(
        ['sysctl', '-w', 'net.bridge.bridge-nf-call-iptables=0'], check=True)
    subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=1'], check=True)


def clean():
    remove_iface()
    remove_bridge()
    remove_ns()


def setup():
    setup_kernel()
    create_ns()
    create_bridge()
    create_iface()
    create_routes()
    ping_all()


def start(namespace: str, binary: str, arguments: list[str], stdout=None, stderr=None, cwd=None, env=None, close_fds=True):
    return NSPopen(namespace, [binary] + arguments, cwd=cwd, env=env, stdout=stdout, stderr=stderr, close_fds=close_fds)
