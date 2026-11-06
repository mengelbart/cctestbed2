
import errno
import shlex
import subprocess

from collections import Counter
from contextlib import ExitStack, contextmanager

from pyroute2 import netns, IPRoute, NetNS, NetlinkError

NAMESPACES = [
    {
        'name': 'ns1',
        'routes': [
            {
                'dst': '10.0.23.0/24',
                'gateway': '10.0.12.2',
            },
            {
                'dst': '10.0.34.0/24',
                'gateway': '10.0.12.2',
            },
        ],
    },
    {
        'name': 'ns2',
        'forwarding': True,
        'routes': [
            {
                'dst': '10.0.34.0/24',
                'gateway': '10.0.23.2',
            },
        ],
    },
    {
        'name': 'ns3',
        'forwarding': True,
        'routes': [
            {
                'dst': '10.0.12.0/24',
                'gateway': '10.0.23.1',
            },
        ],
    },
    {
        'name': 'ns4',
        'routes': [
            {
                'dst': '10.0.12.0/24',
                'gateway': '10.0.34.1',
            },
            {
                'dst': '10.0.23.0/24',
                'gateway': '10.0.34.1',
            },
        ],
    },
]

DEVICES = [
    {
        'dev': 'l12',
        'name-a': 'l12-a',
        'ns-a': 'ns1',
        'ip-a': '10.0.12.1',
        'name-b': 'l12-b',
        'ns-b': 'ns2',
        'ip-b': '10.0.12.2',
        'mask': 24,
        'broadcast': '10.0.12.255',
    },
    {
        'dev': 'l23',
        'name-a': 'l23-a',
        'ns-a': 'ns2',
        'ip-a': '10.0.23.1',
        'name-b': 'l23-b',
        'ns-b': 'ns3',
        'ip-b': '10.0.23.2',
        'mask': 24,
        'broadcast': '10.0.23.255',
    },
    {
        'dev': 'l34',
        'name-a': 'l34-a',
        'ns-a': 'ns3',
        'ip-a': '10.0.34.1',
        'name-b': 'l34-b',
        'ns-b': 'ns4',
        'ip-b': '10.0.34.2',
        'mask': 24,
        'broadcast': '10.0.34.255',
    }
]


# The two router interfaces on the ns2-ns3 link that shape traffic. ns2:l23-a
# is the bottleneck for the ns1 -> ns4 direction, ns3:l23-b for ns4 -> ns1.
BOTTLENECKS = [
    {'ns': 'ns2', 'if': 'l23-a'},
    {'ns': 'ns3', 'if': 'l23-b'},
]

# Qdisc chain on every bottleneck: netem (delay) -> shaper (rate) -> leaf.
DELAY_HANDLE = '1:'
SHAPER_HANDLE = '2:'
HTB_CLASS = '2:10'
LEAF_HANDLE = '3:'

# netem holds every packet for the full delay at the unshaped arrival rate, so
# its queue (in packets) must not become a hidden bottleneck.
NETEM_LIMIT = 1_000_000


def create_ns():
    for ns in NAMESPACES:
        try:
            netns.create(ns['name'])
            with NetNS(ns['name']) as n:
                n.link('set', index=n.link_lookup(ifname='lo')[0], state='up')
        except Exception as e:
            print(e)


def enable_forwarding():
    for ns in NAMESPACES:
        if ns.get('forwarding'):
            subprocess.run(['ip', 'netns', 'exec', ns['name'],
                            'sysctl', '-w', 'net.ipv4.ip_forward=1'], check=True)


def set_sysctls(sysctls: dict[str, dict[str, str | int]]):
    for ns, settings in sysctls.items():
        for key, value in settings.items():
            subprocess.run(['ip', 'netns', 'exec', ns,
                            'sysctl', '-w', f'{key}={value}'], check=True)


def remove_ns():
    for ns in NAMESPACES:
        try:
            netns.remove(ns['name'])
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f'{e}:', type(e).__name__)


OFFLOADS = ['tso', 'gso', 'gro', 'tx-udp-segmentation']


def disable_offloads(namespace, ifname):
    '''Make qdiscs and tcpdump see MTU-sized packets instead of GSO/GRO aggregates.'''
    args = []
    for feature in OFFLOADS:
        args += [feature, 'off']
    subprocess.run(['ip', 'netns', 'exec', namespace,
                    'ethtool', '-K', ifname] + args, check=True)


def create_iface():
    with IPRoute() as ipr:
        for config in DEVICES:
            try:
                # both ends are created in their namespaces, never in the root namespace
                ipr.link('add', ifname=config['name-a'], kind='veth',
                         net_ns_fd=config['ns-a'],
                         peer={'ifname': config['name-b'], 'net_ns_fd': config['ns-b']})
                for side in ('a', 'b'):
                    with NetNS(config[f'ns-{side}']) as ns:
                        index = ns.link_lookup(ifname=config[f'name-{side}'])[0]
                        ns.addr('add', index=index, address=config[f'ip-{side}'],
                                mask=config['mask'], broadcast=config['broadcast'])
                        ns.link('set', index=index, state='up')
                disable_offloads(config['ns-a'], config['name-a'])
                disable_offloads(config['ns-b'], config['name-b'])
            except Exception as e:
                print(f'{type(e).__name__}: {e}, config: {config}')


def create_routes():
    for namespace in NAMESPACES:
        for route in namespace['routes']:
            try:
                with NetNS(namespace['name']) as ns:
                    ns.route('add', dst=route['dst'], gateway=route['gateway'])
            except Exception as e:
                print(e, namespace, route)


@contextmanager
def bottleneck_handles():
    '''Open one NetNS handle per bottleneck namespace, keyed by namespace name.

    The tc helpers below take these handles, so they can run in worker threads
    without forking.
    '''
    with ExitStack() as stack:
        yield {b['ns']: stack.enter_context(NetNS(b['ns'], flags=0))
               for b in BOTTLENECKS}


def _bottleneck_devs(handles):
    for bottleneck in BOTTLENECKS:
        ns = handles[bottleneck['ns']]
        yield ns, ns.link_lookup(ifname=bottleneck['if'])[0]


def set_delay(handles, delay_us=10000, verb='add', limit=NETEM_LIMIT):
    for ns, dev in _bottleneck_devs(handles):
        ns.tc(verb, 'netem', index=dev, handle=DELAY_HANDLE,
              delay=delay_us, limit=limit)


def set_bandwidth_limit(handles, rate='10mbit', limit=5000, burst=10000,
                        verb='add', shaper='tbf'):
    for ns, dev in _bottleneck_devs(handles):
        if shaper == 'htb':
            # the htb qdisc does not support 'change' and has nothing to update
            if verb == 'add':
                ns.tc('add', 'htb', index=dev, handle=SHAPER_HANDLE,
                      parent=DELAY_HANDLE, default=0x10)
            # the rate lives on the leaf class, not on the qdisc
            class_verb = 'add-class' if verb == 'add' else 'change-class'
            ns.tc(class_verb, 'htb', index=dev, handle=HTB_CLASS,
                  parent=SHAPER_HANDLE, rate=rate, ceil=rate,
                  burst=burst, cburst=burst)
        elif shaper == 'tbf':
            ns.tc(verb, 'tbf', index=dev, handle=SHAPER_HANDLE,
                  parent=DELAY_HANDLE, rate=rate, limit=limit, burst=burst)
        else:
            raise ValueError(f'unknown shaper {shaper!r}: expected "tbf" or "htb"')


def remove_bandwidth_limit(handles):
    for ns, dev in _bottleneck_devs(handles):
        ns.tc('del', index=dev, handle=SHAPER_HANDLE, parent=DELAY_HANDLE)


def remove_tc(handles):
    '''Delete the root qdisc and with it the whole tree on every bottleneck.'''
    for ns, dev in _bottleneck_devs(handles):
        try:
            ns.tc('del', index=dev)
        except NetlinkError as e:
            if e.code != errno.ENOENT:
                raise


# Ethernet frame size of a full-size packet, used to turn byte limits into packet limits.
MAX_FRAME = 1514

# dualpi2 memlimit high enough to never bind, so its packet limit applies.
DUALPI2_MEMLIMIT = 1 << 30


def leaf_args(aqm=None, aqm_options=None, limit=5000):
    '''tc arguments for the shaper's leaf qdisc.

    The buffer size comes from `limit` (bytes). bfifo uses it directly.
    dualpi2 gets `limit // MAX_FRAME` packets, which matches bfifo for
    full-size packets. Its byte-based memlimit counts skb->truesize, not packet
    length, so it is set out of the way.
    '''
    options = shlex.split(aqm_options or '')
    if aqm is None:
        return ['bfifo', 'limit', str(limit)]
    if aqm == 'dualpi2':
        packets = max(1, limit // MAX_FRAME)
        return ['dualpi2', 'limit', str(packets),
                'memlimit', str(DUALPI2_MEMLIMIT)] + options
    return [aqm] + options


def set_leaf(aqm=None, aqm_options=None, limit=5000, shaper='tbf'):
    if aqm is not None and shaper != 'htb':
        raise ValueError(
            f'aqm {aqm!r} requires shaper "htb", got {shaper!r}: only htb '
            'supports a leaf class for AQM')
    if shaper != 'htb':
        return
    # pyroute2 has no dualpi2 plugin, so the leaf is configured through the tc
    # command line.
    args = leaf_args(aqm, aqm_options, limit)
    for bottleneck in BOTTLENECKS:
        subprocess.run(
            ['ip', 'netns', 'exec', bottleneck['ns'],
             'tc', 'qdisc', 'replace', 'dev', bottleneck['if'],
             'parent', HTB_CLASS, 'handle', LEAF_HANDLE] + args,
            check=True)


def remove_leaf(shaper='tbf'):
    if shaper != 'htb':
        return
    for bottleneck in BOTTLENECKS:
        subprocess.run(
            ['ip', 'netns', 'exec', bottleneck['ns'],
             'tc', 'qdisc', 'del', 'dev', bottleneck['if'],
             'parent', HTB_CLASS, 'handle', LEAF_HANDLE],
            check=False)


def setup_tc(delay_us=0, bandwidth='1mbit', limit=5000, burst=10000,
             shaper='tbf', aqm=None, aqm_options=None):
    with bottleneck_handles() as handles:
        set_delay(handles, delay_us)
        set_bandwidth_limit(handles, bandwidth, limit=limit, burst=burst,
                            shaper=shaper)
    set_leaf(aqm, aqm_options, limit=limit, shaper=shaper)


def clear_tc():
    if len(netns.listnetns()) == 0:
        return
    with bottleneck_handles() as handles:
        remove_tc(handles)


def ping_all():
    procs = []
    for ns in NAMESPACES:
        for device in DEVICES:
            procs.append(start(ns['name'], 'ping', [
                '-c', '1', '-4', device['ip-a']], stdout=subprocess.PIPE))
            procs.append(start(ns['name'], 'ping', [
                '-c', '1', '-4', device['ip-b']], stdout=subprocess.PIPE))
    for proc in procs:
        ret = proc.wait()
        if ret != 0:
            print(
                f'WARNING: ping command exited with non-zero exit code: {ret} ({proc.args})')


def setup_kernel():
    for module in ['sch_netem', 'sch_tbf', 'sch_htb']:
        subprocess.run(['modprobe', module], check=True)
    for module in ['sch_dualpi2']:
        # not fatal: a kernel may lack dualpi2 and still run non-AQM testcases
        if subprocess.run(['modprobe', module], check=False).returncode != 0:
            print(f'WARNING: failed to load {module}')


def clean():
    remove_ns()


def setup():
    clean()
    setup_kernel()
    create_ns()
    enable_forwarding()
    create_iface()
    create_routes()
    ping_all()


def capture_points():
    '''(namespace, interface, pcap file) for every veth end.

    Namespaces with a single interface keep the name `<ns>.pcap`, the others
    get `<ns>-<interface>.pcap`.
    '''
    ends = [(d[f'ns-{side}'], d[f'name-{side}'])
            for d in DEVICES for side in ('a', 'b')]
    per_ns = Counter(ns for ns, _ in ends)
    return [(ns, ifname, f'{ns}.pcap' if per_ns[ns] == 1 else f'{ns}-{ifname}.pcap')
            for ns, ifname in ends]


def start(namespace: str, binary: str, arguments: list[str], stdout=None, stderr=None, cwd=None, env=None, close_fds=True):
    '''Start binary in namespace. `ip netns exec` execs in place, so the pid is that of binary.'''
    return subprocess.Popen(['ip', 'netns', 'exec', namespace, binary] + arguments,
                            cwd=cwd, env=env, stdout=stdout, stderr=stderr, close_fds=close_fds)
