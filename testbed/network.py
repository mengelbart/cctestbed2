
import subprocess

from pyroute2 import netns, IPRoute, NSPopen, NetNS

NAMESPACES = [
    {
        'name': 'ns1',
        'routes': [
            {
                'dst': '10.0.23.0/24',
                'gateway': '10.0.12.2',
                'if': 'l12-a',
            },
            {
                'dst': '10.0.34.0/24',
                'gateway': '10.0.12.2',
                'if': 'l12-a',
            },
        ],
    },
    {
        'name': 'ns2',
        'routes': [
            {
                'dst': '10.0.34.0/24',
                'gateway': '10.0.23.2',
                'if': 'l23-a',
            },
        ],
    },
    {
        'name': 'ns3',
        'routes': [
            {
                'dst': '10.0.12.0/24',
                'gateway': '10.0.23.1',
                'if': 'l23-b',
            },
        ],
    },
    {
        'name': 'ns4',
        'routes': [
            {
                'dst': '10.0.12.0/24',
                'gateway': '10.0.34.1',
                'if': 'l34-b',
            },
            {
                'dst': '10.0.23.0/24',
                'gateway': '10.0.34.1',
                'if': 'l34-b',
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


def create_ns():
    for ns in NAMESPACES:
        try:
            netns.create(ns['name'])
            with NetNS(ns['name']) as n:
                n.link('set', index=n.link_lookup(ifname='lo')[0], state='up')
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


def create_iface():
    with IPRoute() as ipr:
        for config in DEVICES:
            try:
                ipr.link('add', ifname=config['name-a'], kind='veth', peer=config['name-b'])
                a = ipr.link_lookup(ifname=config['name-a'])[0]
                b = ipr.link_lookup(ifname=config['name-b'])[0]
                ipr.link('set', index=a, net_ns_fd=config['ns-a'])
                ipr.link('set', index=b, net_ns_fd=config['ns-b'])
                with NetNS(config['ns-a']) as ns:
                    ns.addr('add', index=a, address=config['ip-a'], mask=config['mask'], broadcast=config['broadcast'])
                    ns.link('set', index=a, state='up')
                with NetNS(config['ns-b']) as ns:
                    ns.addr('add', index=b, address=config['ip-b'], mask=config['mask'], broadcast=config['broadcast'])
                    ns.link('set', index=b, state='up')
            except Exception as e:
                print(f'{type(e).__name__}: {e}, config: {config}')


def remove_iface():
    with IPRoute() as ipr:
        for config in DEVICES:
            try:
                devs = ipr.link_lookup(ifname=config['name-a'])
                peers = ipr.link_lookup(ifname=config['name-b'])
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
                with NetNS(namespace['name']) as ns:
                    ns.route('add', dst=route['dst'], gateway=route['gateway'])
            except Exception as e:
                print(e, namespace, route)


def set_delay(delay_us=10000, verb='add'):
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='l12-b')[0]
        ns.tc(verb, 'netem', index=dev, handle='1:', delay=delay_us)
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='l34-a')[0]
        ns.tc(verb, 'netem', index=dev, handle='1:', delay=delay_us)


def remove_delay():
    if len(netns.listnetns()) == 0:
        return
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='l12-b')[0]
        ns.tc('del', 'netem', index=dev, handle='1:')
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='l34-a')[0]
        ns.tc('del', 'netem', index=dev, handle='1:')


def set_bandwidth_limit(rate='10mbit', limit=5000, burst=10000, verb='add'):
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='l12-b')[0]
        ns.tc(verb, 'tbf', index=dev, handle='0:', parent='1:', rate=rate,
              limit=limit, burst=burst)
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='l34-a')[0]
        ns.tc(verb, 'tbf', index=dev, handle='0:', parent='1:', rate=rate,
              limit=limit, burst=burst)


def remove_bandwidth_limit():
    if len(netns.listnetns()) == 0:
        return
    with NetNS('ns2') as ns:
        dev = ns.link_lookup(ifname='l12-b')[0]
        ns.tc('del', index=dev, handle='0:', parent='1:')
    with NetNS('ns3') as ns:
        dev = ns.link_lookup(ifname='l34-a')[0]
        ns.tc('del', index=dev, handle='0:', parent='1:')


def setup_tc(delay_us=0, bandwidth='1mbit'):
    set_delay(delay_us)
    set_bandwidth_limit(bandwidth)


def clear_tc():
    try:
        remove_bandwidth_limit()
    except Exception as e:
        print(f'Error occurred while removing bandwidth limit: {e}')
    try:
        remove_delay()
    except Exception as e:
        print(f'Error occurred while removing delay: {e}')


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
        proc.release()


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
    remove_ns()


def setup():
    setup_kernel()
    create_ns()
    create_iface()
    create_routes()
    ping_all()


def start(namespace: str, binary: str, arguments: list[str], stdout=None, stderr=None, cwd=None, env=None, close_fds=True):
    return NSPopen(namespace, [binary] + arguments, cwd=cwd, env=env, stdout=stdout, stderr=stderr, close_fds=close_fds)
