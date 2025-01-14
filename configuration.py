import os
import yaml

from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path


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
    bandwidth: Optional[float]
    latency: Optional[float]
    delay: Optional[float]


@dataclass
class Testcase:
    name: str
    network: list[NetworkConfig]
    applications: list[ApplicationConfig]
    duration: int


def load_config(yaml_file: str) -> Testcase:
    with open(yaml_file, 'r') as file:
        config_data = yaml.safe_load(file)

    network_config = [NetworkConfig(
        **net_conf) for net_conf in config_data.get('network', [])]

    apps = []
    for app in config_data['applications']:
        apps.append(ApplicationConfig(
            name=app['name'],
            namespace=app['namespace'],
            start_time=app['start_time'],
            duration=app['duration'],
            environment=[EnvVariable(**env_var)
                         for env_var in app.get('environment', [])],
            binary=os.path.abspath(app['binary']),
            arguments=app.get('arguments', []),
        ))

    return Testcase(
        name=Path(yaml_file).stem,
        network=network_config,
        applications=apps,
        duration=config_data['duration'],
    )
