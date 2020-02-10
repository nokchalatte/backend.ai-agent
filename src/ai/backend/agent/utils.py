import asyncio
from decimal import Decimal
import io
import ipaddress
import json
import logging
from pathlib import Path
import re
from typing import (
    Any, Optional,
    Callable, Iterable,
    Mapping, MutableMapping,
    List, Sequence, Tuple, Union,
    Type, overload,
    Set,
)
from typing_extensions import Final
from uuid import UUID

import aiodocker
from aiodocker.docker import DockerContainer
import netifaces
import trafaret as t

from ai.backend.common import identity
from ai.backend.common.etcd import AsyncEtcd
from ai.backend.common.logging import BraceStyleAdapter
from ai.backend.common.types import (
    PID, HostPID, ContainerPID, KernelId,
    ServicePort,
    ServicePortProtocols,
)

log = BraceStyleAdapter(logging.getLogger('ai.backend.agent.utils'))

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

InOtherContainerPID: Final = ContainerPID(PID(-2))
NotContainerPID: Final = ContainerPID(PID(-1))
NotHostPID: Final = HostPID(PID(-1))

_rx_service_ports = re.compile(r'^(?P<name>\w+):(?P<proto>\w+):(?P<ports>\[\d+(?:,\d+)*\]|\d+)(?:,|$)')


def update_nested_dict(dest: MutableMapping, additions: Mapping) -> None:
    for k, v in additions.items():
        if k not in dest:
            dest[k] = v
        else:
            if isinstance(dest[k], MutableMapping):
                assert isinstance(v, MutableMapping)
                update_nested_dict(dest[k], v)
            elif isinstance(dest[k], Sequence):
                assert isinstance(v, Sequence)
                dest[k].extend(v)
            else:
                dest[k] = v


current_loop: Callable[[], asyncio.AbstractEventLoop]
if hasattr(asyncio, 'get_running_loop'):
    current_loop = asyncio.get_running_loop  # type: ignore
else:
    current_loop = asyncio.get_event_loop    # type: ignore


def numeric_list(s: str) -> List[int]:
    return [int(p) for p in s.split()]


def parse_service_ports(s: str) -> Sequence[ServicePort]:
    items: List[ServicePort] = []
    used_ports: Set[int] = set()
    while True:
        match = _rx_service_ports.search(s)
        if match:
            s = s[len(match.group(0)):]
            name = match.group('name')
            if not name:
                raise ValueError('Service port name must be not empty.')
            protocol = match.group('proto')
            if protocol == 'pty':
                # unsupported, skip
                continue
            if protocol not in ('tcp', 'http'):
                raise ValueError(f'Unsupported service port protocol: {protocol}')
            ports = tuple(map(int, match.group('ports').strip('[]').split(',')))
            for p in ports:
                if p in used_ports:
                    raise ValueError(f'The port {p} is already used by another service port.')
                if p <= 1024:
                    raise ValueError(f'The service port number {p} must be larger than 1024.')
                if p in (2000, 2001, 2002, 2003):
                    raise ValueError('The service ports 2000 to 2003 are reserved for internal use.')
                used_ports.add(p)
            items.append({
                'name': name,
                'protocol': ServicePortProtocols(protocol),
                'container_ports': ports,
                'host_ports': (None,) * len(ports),
            })
        else:
            break
        if not s:
            break
    return items


def remove_exponent(num: Decimal) -> Decimal:
    return num.quantize(Decimal(1)) if num == num.to_integral() else num.normalize()


@overload
def read_sysfs(path: Union[str, Path], type_: Type[bool], default: bool) -> bool:
    ...


@overload
def read_sysfs(path: Union[str, Path], type_: Type[int], default: int) -> int:
    ...


@overload
def read_sysfs(path: Union[str, Path], type_: Type[float], default: float) -> float:
    ...


@overload
def read_sysfs(path: Union[str, Path], type_: Type[str], default: str) -> str:
    ...


def read_sysfs(path: Union[str, Path], type_: Type[Any], default: Any = None) -> Any:
    def_vals: Mapping[Any, Any] = {
        bool: False,
        int: 0,
        float: 0.0,
        str: '',
    }
    if type_ not in def_vals:
        raise TypeError('unsupported conversion type from sysfs content')
    if default is None:
        default = def_vals[type_]
    try:
        raw_str = Path(path).read_text().strip()
        if type_ is bool:
            return t.ToBool().check(raw_str)
        else:
            return type_(raw_str)
    except IOError:
        return default


async def read_tail(path: Path, nbytes: int) -> bytes:
    file_size = path.stat().st_size

    def _read_tail() -> bytes:
        with open(path, 'rb') as f:
            f.seek(max(file_size - nbytes, 0), io.SEEK_SET)
            return f.read(nbytes)

    loop = current_loop()
    return await loop.run_in_executor(None, _read_tail)


async def get_kernel_id_from_container(val: Union[str, DockerContainer]) -> Optional[KernelId]:
    if isinstance(val, DockerContainer):
        if 'Name' not in val._container:
            await val.show()
        name = val['Name']
    elif isinstance(val, str):
        name = val
    name = name.lstrip('/')
    if not name.startswith('kernel.'):
        return None
    try:
        return KernelId(UUID(name.rsplit('.', 2)[-1]))
    except (IndexError, ValueError):
        return None


async def get_subnet_ip(etcd: AsyncEtcd, network: str, fallback_addr: str = '0.0.0.0') -> str:
    subnet = await etcd.get(f'config/network/subnet/{network}')
    if subnet is None:
        addr = fallback_addr
    else:
        subnet = ipaddress.ip_network(subnet)
        if subnet.prefixlen == 0:
            addr = fallback_addr
        else:
            local_ipaddrs = [*identity.fetch_local_ipaddrs(subnet)]
            log.debug('get_subnet_ip(): subnet {} candidates: {}',
                      subnet, local_ipaddrs)
            if local_ipaddrs:
                addr = str(local_ipaddrs[0])
            else:
                addr = fallback_addr
    return addr


async def host_pid_to_container_pid(container_id: str, host_pid: HostPID) -> ContainerPID:
    kernel_ver = Path('/proc/version').read_text()
    if m := re.match(r'Linux version (\d+)\.(\d+)\..*', kernel_ver):  # noqa
        kernel_ver_tuple: Tuple[str, str] = m.groups()  # type: ignore
        if kernel_ver_tuple < ('4', '1'):
            # TODO: this should be deprecated when the minimun supported Linux kernel will be 4.1.
            #
            # In CentOs 7, NSPid is not accesible since it is supported from Linux kernel >=4.1.
            # We provide alternative, although messy, way for older Linux kernels. Below describes
            # the logic briefly:
            #   * Obtain information on all the processes inside the target container,
            #     which contains host PID, by docker top API (containers/<container-id>/top).
            #     - Get the COMMAND of the target process (by using host_pid).
            #     - Filter host processes which have the exact same COMMAND.
            #   * Obtain information on all the processes inside the target container,
            #     which contains container PID, by executing "ps -aux" command from inside the container.
            #     - Filter container processes which have the exact same COMMAND.
            #   * Get the index of the target process from the host process table.
            #   * Use the index to get the target process from the container process table, and get PID.
            #     - Since docker top and ps -aux both displays processes in the order of PID, we
            #       can safely assume that the order of the processes from both tables are the same.
            #
            # Example host and container process table:
            #
            # [
            #   ['devops', '15454', '12942', '99', '15:36', 'pts/1', '00:00:08', 'python mnist.py'],
            #   ... (processes with the same COMMAND)
            # ]
            #
            # [
            #   ['work', '227', '121', '4.6', '22408680', '1525428', 'pts/1', 'Rl+', '06:36', '0:08',
            #    'python', 'mnist.py'],
            #   ... (processes with the same COMMAND)
            # ]
            try:
                docker = aiodocker.Docker()
                # Get process table from host (docker top information). Filter processes which have
                # exactly the same COMMAND as with target host process.
                result = await docker._query_json(f'containers/{container_id}/top', method='GET')
                procs = result['Processes']
                cmd = list(filter(lambda x: str(host_pid) == x[1], procs))[0][7]
                host_table = list(filter(lambda x: cmd == x[7], procs))

                # Get process table from inside container (execute 'ps -aux' command from container).
                # Filter processes which have exactly the same COMMAND like above.
                result = await docker._query_json(
                    f'containers/{container_id}/exec',
                    method='POST',
                    data={
                        'AttachStdin': False,
                        'AttachStdout': True,
                        'AttachStderr': True,
                        'Cmd': ['ps', '-aux'],
                    }
                )
                exec_id = result['Id']
                async with docker._query(
                    f'exec/{exec_id}/start',
                    method='POST',
                    headers={'content-type': 'application/json'},
                    data=json.dumps({
                        'Stream': False,  # get response immediately
                        'Detach': False,
                        'Tty': False,
                    }),
                ) as resp:
                    result = await resp.read()
                    result = result.decode('latin-1').split('\n')
                result = list(map(lambda x: x.split(), result))
                head = result[0]
                procs = result[1:]
                pid_idx, cmd_idx = head.index('PID'), head.index('COMMAND')
                container_table = list(
                    filter(lambda x: cmd == ' '.join(x[cmd_idx:]) if x else False, procs)
                )

                # When there are multiple processes which have the same COMMAND, just get the index of
                # the target host process and apply it with the container table. Since ps and docker top
                # both displays processes ordered by PID, we can expect those two tables have same
                # order of processes.
                process_idx = None
                for idx, p in enumerate(host_table):
                    if str(host_pid) == p[1]:
                        process_idx = idx
                        break
                else:
                    raise IndexError
                container_pid = ContainerPID(container_table[process_idx][pid_idx])
                log.debug('host pid {} is mapped to container pid {}', host_pid, container_pid)
                return ContainerPID(PID(container_pid))
            except asyncio.CancelledError:
                raise
            except (IndexError, KeyError, aiodocker.exceptions.DockerError):
                return NotContainerPID
            finally:
                await docker.close()

    try:
        for p in Path('/sys/fs/cgroup/pids/docker').iterdir():
            if not p.is_dir():
                continue
            tasks_path = p / 'tasks'
            cgtasks = [*map(int, tasks_path.read_text().splitlines())]
            if host_pid not in cgtasks:
                continue
            if p.name == container_id:
                proc_path = Path(f'/proc/{host_pid}/status')
                proc_status = {k: v for k, v
                               in map(lambda l: l.split(':\t'),
                                      proc_path.read_text().splitlines())}
                nspids = [*map(lambda pid: ContainerPID(PID(int(pid))), proc_status['NSpid'].split())]
                return nspids[1]
            return InOtherContainerPID
        return NotContainerPID
    except (ValueError, KeyError, IOError):
        return NotContainerPID


async def container_pid_to_host_pid(container_id: str, container_pid: ContainerPID) -> HostPID:
    # TODO: implement
    return NotHostPID


def fetch_local_ipaddrs(cidr: IPNetwork) -> Iterable[IPAddress]:
    ifnames = netifaces.interfaces()
    proto = netifaces.AF_INET if cidr.version == 4 else netifaces.AF_INET6
    for ifname in ifnames:
        addrs = netifaces.ifaddresses(ifname).get(proto, None)
        if addrs is None:
            continue
        for entry in addrs:
            addr = ipaddress.ip_address(entry['addr'])
            if addr in cidr:
                yield addr
