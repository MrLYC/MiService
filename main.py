#!/usr/bin/env python3
from turtle import pd
from aiohttp import ClientSession
import asyncio
import logging
import os
import sys
from pathlib import Path
from argparse import ArgumentParser
from typing import List
from miservice import MiAccount, MiIOService
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Iterable
import cattrs
from miservice import MiIOService
from pprint import pprint

from aioprometheus import REGISTRY, Gauge
from aioprometheus.service import Service as PrometheusService

MI_STATUS = Gauge(
    "mi_device_status",
    "Mi device status",
    None,
    REGISTRY,
)


@dataclass
class DeviceInfo:
    name: str
    model: str
    did: str
    token: str


@dataclass
class ServiceInfo:
    iid: int
    type: str
    description: str
    access: Optional[List[str]] = None
    properties: Optional[List["ServiceInfo"]] = None

    @classmethod
    def _cattrs_structure_hook(cls, data, type_):
        info = ServiceInfo(
            iid=data["iid"],
            type=data["type"],
            description=data["description"],
            access=data.get("access"),
        )

        if "properties" in data:
            info.properties = [
                cls._cattrs_structure_hook(prop, cls) for prop in data["properties"]
            ]

        return info


cattrs.register_structure_hook(ServiceInfo, ServiceInfo._cattrs_structure_hook)


@dataclass
class SepcInfo:
    type: str
    description: str
    services: List[ServiceInfo]

    def get_props_by_desc(self, *descs: str) -> List[str]:
        props = []
        services = self.services
        for desc in descs:
            for service in services:
                if service.description == desc:
                    props.append(service.iid)
                    services = service.properties
                    break

        return props

    @classmethod
    def _cattrs_structure_hook(cls, data, type_):
        return cls(
            type=data["type"],
            description=data["description"],
            services=[
                ServiceInfo._cattrs_structure_hook(service, ServiceInfo)
                for service in data["services"]
            ],
        )


cattrs.register_structure_hook(SepcInfo, SepcInfo._cattrs_structure_hook)


@dataclass
class Device:
    info: DeviceInfo
    spec: SepcInfo

    def get_props_by_desc(self, *descs: str) -> List[str]:
        props = []
        services = self.spec.services
        for desc in descs:
            for spec in services:
                if spec.description == desc:
                    props.append(spec.iid)
                    services = spec.properties
                    break

        return props


@dataclass
class Context:
    devices: Dict[str, Dict[str, DeviceInfo]] = field(default_factory=dict)
    specs: Dict[str, SepcInfo] = field(default_factory=dict)

    async def init_devices(self, service: MiIOService):
        result = await service.device_list()
        for device in cattrs.structure(result, List[DeviceInfo]):
            if device.model not in self.devices:
                self.devices[device.model] = {device.did: device}
            else:
                self.devices[device.model][device.did] = device

        return self.devices

    async def get_spec(self, service: MiIOService, model: str) -> SepcInfo:
        if model in self.specs:
            return self.specs[model]

        result = await service.miot_spec(model, "json")
        self.specs[model] = cattrs.structure(result, SepcInfo)
        return self.specs[model]

    async def iter_devices_by_model(self, model: str) -> Iterable[DeviceInfo]:
        devices = self.devices.get(model)
        if not devices:
            return

        return devices.values()


async def collect(
    context: Context, service: MiIOService, model: str, descs: List[List[str]]
):
    spec = await context.get_spec(service, model)
    props = {}
    for desc in descs:
        result = spec.get_props_by_desc(*desc)
        if len(result) != len(desc):
            continue
        props["/".join(desc)] = result

    for device in await context.iter_devices_by_model(model):
        values = await service.miot_get_props(
            device.did, [i for i in props.values() if i]
        )
        for value, key in zip(values, props):
            MI_STATUS.set(
                {
                    "name": device.name,
                    "did": device.did,
                    "model": device.model,
                    "key": key.lower(),
                },
                float(value) if value else float("nan"),
            )


async def collect_zhimi_airpurifier_ma4(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "zhimi.airpurifier.ma4",
        [
            ["Air Purifier", "Switch Status"],
            ["Air Purifier", "Mode"],
            ["Environment", "Temperature"],
            ["Environment", "Relative Humidity"],
            ["Environment", "PM2.5 Density"],
            ["Environment", "Relative Humidity"],
            ["Filter", "Filter Life Level"],
            ["Filter", "Filter Used Time"],
            ["aqi", "average-aqi"],
        ],
    )


async def collect_zhimi_airpurifier_m1(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "zhimi.airpurifier.m1",
        [
            ["Air Purifier", "Switch Status"],
            ["Air Purifier", "Mode"],
            ["Environment", "Indoor Temperature"],
            ["Environment", "Relative Humidity"],
            ["Environment", "PM2.5 Density"],
            ["Filter", "Filter Life Level"],
            ["Filter", "Filter Used Time"],
        ],
    )


async def collect_magnet_sensor(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "isa.magnet.dw2hl",
        [
            ["Magnet Sensor", "Illumination"],
            ["Magnet Sensor", "Contact State"],
            ["Battery", "Battery Level"],
        ],
    )


async def collect_chuangmi_camera_v2(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "chuangmi.camera.v2",
        [
            ["Camera Control", "Switch Status"],
        ],
    )


async def collect_chuangmi_plug_m1(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "chuangmi.plug.m1",
        [
            ["Switch", "Switch Status"],
            ["Switch", "Temperature"],
        ],
    )


async def collect_cgllc_motion(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "cgllc.motion.cgpr1",
        [
            ["Motion Sensor", "Illumination"],
            ["Motion Sensor", "No Motion Duration"],
            ["Battery", "Battery Level"],
        ],
    )


async def collect_lumi_sensor(context: Context, service: MiIOService):
    await collect(
        context,
        service,
        "lumi.sensor_ht.v1",
        [
            ["Temperature Humidity Sensor", "Temperature"],
            ["Temperature Humidity Sensor", "Relative Humidity"],
        ],
    )


async def collect_mode(context: Context, user_id: str, password: str, config: str):
    async with ClientSession() as session:
        account = MiAccount(session, user_id, password, config)
        service = MiIOService(account)

        await collect_zhimi_airpurifier_ma4(context, service)
        await collect_zhimi_airpurifier_m1(context, service)
        await collect_magnet_sensor(context, service)
        await collect_chuangmi_camera_v2(context, service)
        await collect_chuangmi_plug_m1(context, service)
        await collect_cgllc_motion(context, service)
        await collect_lumi_sensor(context, service)


async def spec_mode(context: Context, service: MiIOService, model: str):
    spec = await context.get_spec(service, model)
    pprint(cattrs.unstructure(spec))


async def main(args):
    context = Context()
    async with ClientSession() as session:
        account = MiAccount(session, args.user_id, args.password, args.config)
        service = MiIOService(account)

        await context.init_devices(service)
        if args.spec is not None:
            await spec_mode(context, service, args.spec)
            return

    prom = PrometheusService()
    await prom.start(addr=args.addr, port=args.port)
    while True:
        try:
            await collect_mode(context, args.user_id, args.password, args.config)
        except Exception as e:
            print(e)
        except KeyboardInterrupt:
            break

        await asyncio.sleep(args.interval)

    await prom.stop()


if __name__ == "__main__":
    parser = ArgumentParser()

    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    parser.add_argument("-u", "--user_id", help="user id")
    parser.add_argument("-p", "--password", help="password")
    parser.add_argument("-a", "--addr", default="localhost", help="prometheus addr")
    parser.add_argument("-P", "--port", default=8080, help="prometheus port")
    parser.add_argument("-i", "--interval", default=60, help="collect interval")
    parser.add_argument("-s", "--spec", default=None, help="collect spec")
    parser.add_argument(
        "-c",
        "--config",
        default=os.path.join(str(Path.home()), ".mi.token"),
        help="config file",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stdout
    )

    asyncio.run(main(args))
