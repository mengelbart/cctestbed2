import os
import yaml
import shutil

from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

from .network import MAX_FRAME, NAMESPACES, NETEM_LIMIT


@dataclass
class EnvVariable:
    name: str
    value: str


@dataclass
class ApplicationConfig:
    name: str
    namespace: str
    start_time: int
    duration: int
    environment: List[EnvVariable]
    binary: str
    arguments: List[str]


@dataclass
class NetworkConfig:
    traffic_control: bool
    duration: Optional[int]
    bandwidth: Optional[str]
    burst: Optional[int]
    limit: Optional[int]
    delay: Optional[float]
    shaper: str = "tbf"  # 'tbf' or 'htb'
    aqm: Optional[str] = None  # Leaf qdisc of the shaper, e.g. 'dualpi2'
    # Tuning options for `aqm`, passed to tc verbatim. Buffer size is derived
    # from `limit`, see network.leaf_args.
    aqm_options: Optional[str] = None
    netem_limit: int = NETEM_LIMIT  # packets


@dataclass
class Testcase:
    name: str
    network: list[NetworkConfig]
    applications: list[ApplicationConfig]
    duration: int


SHAPERS = ('tbf', 'htb')
APP_KEYS = {'name', 'namespace', 'start_time', 'duration', 'environment',
            'binary', 'arguments'}


class ConfigError(Exception):
    pass


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(testcase: Testcase) -> list[str]:
    '''Returns a description of every problem found in testcase.'''
    errors = []
    if not _is_number(testcase.duration) or testcase.duration < 0:
        errors.append(f'duration: must be a number >= 0, got {testcase.duration!r}')

    for i, net in enumerate(testcase.network):
        where = f'network[{i}]'
        if not isinstance(net.traffic_control, bool):
            errors.append(f'{where}.traffic_control: must be true or false')
        if not _is_number(net.duration) or net.duration < 0:
            errors.append(f'{where}.duration: must be a number >= 0, got {net.duration!r}')
        if not net.traffic_control:
            continue
        if not isinstance(net.bandwidth, str) or not net.bandwidth:
            errors.append(f'{where}.bandwidth: must be a tc rate string like "10mbit", got {net.bandwidth!r}')
        if not _is_number(net.delay) or net.delay < 0:
            errors.append(f'{where}.delay: must be a number of microseconds >= 0, got {net.delay!r}')
        if not _is_int(net.limit) or net.limit < MAX_FRAME:
            errors.append(f'{where}.limit: must be an integer >= {MAX_FRAME} bytes (one full-size frame), got {net.limit!r}')
        if not _is_int(net.burst) or net.burst < MAX_FRAME:
            errors.append(f'{where}.burst: must be an integer >= {MAX_FRAME} bytes (one full-size frame), got {net.burst!r}')
        if not _is_int(net.netem_limit) or net.netem_limit < 1:
            errors.append(f'{where}.netem_limit: must be an integer >= 1, got {net.netem_limit!r}')
        if net.shaper not in SHAPERS:
            errors.append(f'{where}.shaper: must be one of {SHAPERS}, got {net.shaper!r}')
        elif net.aqm is not None and net.shaper != 'htb':
            errors.append(f'{where}.aqm: requires shaper "htb", got {net.shaper!r}')

    namespaces = [ns['name'] for ns in NAMESPACES]
    names = set()
    for i, app in enumerate(testcase.applications):
        where = f'applications[{i}] ({app.name})'
        if app.name in names:
            errors.append(f'{where}.name: duplicate, log files would overwrite each other')
        names.add(app.name)
        if app.namespace not in namespaces:
            errors.append(f'{where}.namespace: must be one of {namespaces}, got {app.namespace!r}')
        for key in ('start_time', 'duration'):
            value = getattr(app, key)
            if not _is_number(value) or value < 0:
                errors.append(f'{where}.{key}: must be a number >= 0, got {value!r}')
        if not all(isinstance(a, str) for a in app.arguments):
            errors.append(f'{where}.arguments: must all be strings, quote numbers in YAML')
    return errors


def load_config(yaml_file: str) -> Testcase:
    '''Load and validate a testcase. Raises ConfigError listing every problem found.'''
    try:
        with open(yaml_file, "r") as file:
            config_data = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f'{yaml_file}: {e}')
    if not isinstance(config_data, dict):
        raise ConfigError(f'{yaml_file}: expected a mapping at the top level')

    errors = []
    network_config = []
    for i, net_conf in enumerate(config_data.get("network") or []):
        try:
            network_config.append(NetworkConfig(**net_conf))
        except TypeError as e:
            errors.append(f'network[{i}]: {e}')

    apps = []
    for i, app in enumerate(config_data.get("applications") or []):
        unknown = set(app) - APP_KEYS
        if unknown:
            errors.append(f'applications[{i}]: unknown keys {sorted(unknown)}')
        missing = APP_KEYS - {'environment', 'arguments'} - set(app)
        if missing:
            errors.append(f'applications[{i}]: missing keys {sorted(missing)}')
            continue
        binary = shutil.which(app["binary"])
        if binary is None:
            errors.append(f'applications[{i}] ({app["name"]}).binary: {app["binary"]!r} not found')
            continue
        try:
            environment = [EnvVariable(**env_var) for env_var in app.get("environment") or []]
        except TypeError as e:
            errors.append(f'applications[{i}] ({app["name"]}).environment: {e}')
            continue
        apps.append(
            ApplicationConfig(
                name=app["name"],
                namespace=app["namespace"],
                start_time=app["start_time"],
                duration=app["duration"],
                environment=environment,
                binary=os.path.abspath(binary),
                arguments=app.get("arguments") or [],
            )
        )

    if "duration" not in config_data:
        errors.append('duration: missing')

    testcase = Testcase(
        name=Path(yaml_file).stem,
        network=network_config,
        applications=apps,
        duration=config_data.get("duration"),
    )
    errors += validate(testcase)
    if errors:
        raise ConfigError('\n'.join(f'{yaml_file}: {e}' for e in errors))
    return testcase
