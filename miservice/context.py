from dataclasses import dataclass, field
from typing import List, Optional, Dict
import cattrs

from miservice import MiIOService


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
    devices: Dict[str, DeviceInfo] = field(default_factory=dict)
    specs: Dict[str, SepcInfo] = field(default_factory=dict)

    async def init_devices(self, service: MiIOService):
        result = await service.device_list()
        self.devices.update(
            {
                device.did: device
                for device in cattrs.structure(result, List[DeviceInfo])
            }
        )

        return self.devices

    async def get_specs(self, service: MiIOService, model: str) -> ServiceInfo:
        if model in self.specs:
            return self.specs[model]

        result = await service.miot_spec(model, "json")
        self.specs[model] = cattrs.structure(result, SepcInfo)
        return self.specs[model]

    async def get_device(self, service: MiIOService, did: str) -> Optional[Device]:
        info = self.devices.get(did)
        if info is None:
            return None

        return Device(info=info, spec=await self.get_specs(service, info.model))
