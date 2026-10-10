"""在局域网里发现 DLNA 投影仪，并控制它播放。"""

from __future__ import annotations

import html
import os
import re
import socket
import struct
import subprocess
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

CREATE_NO_WINDOW = 0x08000000


def _hidden() -> dict:
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:MediaRenderer:2",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "upnp:rootdevice",
)
XIAOMI_HINTS = (
    "xiaomi",
    "redmi",
    "miio",
    "miproj",
    "fengmi",
    "formovie",
    "小米",
    "米家",
    "峰米",
)


class DlnaError(Exception):
    pass


@dataclass
class NetIf:
    name: str
    ip: str
    mask: str

    @property
    def label(self) -> str:
        ip_i = _ip_int(self.ip)
        mask_i = _ip_int(self.mask)
        net = ip_i & mask_i
        prefix = bin(mask_i).count("1")
        return f"{socket.inet_ntoa(struct.pack('!I', net))}/{prefix}"


@dataclass
class Device:
    name: str
    host: str
    model: str
    manufacturer: str
    location: str
    control_url: str
    udn: str

    @property
    def is_xiaomi(self) -> bool:
        blob = " ".join((self.name, self.model, self.manufacturer, self.location))
        low = blob.lower()
        return any(hint in blob or hint in low for hint in XIAOMI_HINTS)

    @property
    def subtitle(self) -> str:
        model = (self.model or "").strip()
        generic = "mediarenderer" in model.lower() or model.lower() in {"dmr", "media renderer"}
        if model and not generic and model not in self.name:
            return f"{model}  ·  {self.host}"
        return self.host


def _ip_int(ip: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip))[0]


def _direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen_direct(req: urllib.request.Request, timeout: float = 3):
    return _direct_opener().open(req, timeout=timeout)


def _interfaces_posix() -> list[NetIf]:
    """macOS 上用默认路由和 ifconfig 找真正上网的那张网卡。"""
    preferred = ""
    try:
        route = subprocess.check_output(["route", "-n", "get", "default"], text=True, timeout=4)
    except Exception:
        route = ""
    for line in route.splitlines():
        if "interface:" in line:
            preferred = line.split(":", 1)[1].strip()
            break
    try:
        raw = subprocess.check_output(["ifconfig"], text=True, timeout=4)
    except Exception:
        return []
    found: list[NetIf] = []
    for block in re.split(r"\n(?=\S)", raw):
        lines = block.splitlines()
        if not lines or ":" not in lines[0]:
            continue
        name = lines[0].split(":", 1)[0]
        if preferred and name != preferred:
            continue
        if any(name.startswith(prefix) for prefix in ("lo", "utun", "awdl", "llw", "bridge", "vmnet", "gif", "stf")):
            continue
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", block)
        if not match:
            continue
        ip = match.group(1)
        if ip.startswith("127.") or ip.startswith("198.18.") or ip.startswith("198.19."):
            continue
        mask = socket.inet_ntoa(struct.pack("!I", int(match.group(2), 16)))
        found.append(NetIf(name, ip, mask))
        if preferred:
            break
    return found


def local_interfaces() -> list[NetIf]:
    """读取真正接在路由器上的 IPv4 网卡，跳过代理和虚拟网卡。"""
    if os.name != "nt":
        return _interfaces_posix()
    try:
        raw = subprocess.check_output(["ipconfig"], **_hidden())
    except Exception:
        return []
    text = raw.decode("gbk", errors="replace")
    sections: list[dict] = []
    current: dict | None = None
    reading_gw = False
    ip_re = re.compile(r"\d+\.\d+\.\d+\.\d+")

    for line in text.splitlines():
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):
            current = {"name": line.rstrip()[:-1].strip(), "ips": [], "masks": [], "gateways": []}
            sections.append(current)
            reading_gw = False
            continue
        if current is None:
            continue
        if "IPv4" in line or "IP Address" in line:
            current["ips"].extend(ip_re.findall(line))
            reading_gw = False
            continue
        if "子网掩码" in line or "Subnet Mask" in line:
            current["masks"].extend(ip_re.findall(line))
            reading_gw = False
            continue
        if "默认网关" in line or "Default Gateway" in line:
            reading_gw = True
            current["gateways"].extend(ip_re.findall(line))
            continue
        if reading_gw:
            stripped = line.strip()
            if stripped and ip_re.fullmatch(stripped):
                current["gateways"].append(stripped)
                continue
            if stripped:
                reading_gw = False

    found: list[NetIf] = []
    for sec in sections:
        if not sec["ips"] or not sec["masks"]:
            continue
        ip = sec["ips"][0]
        mask = sec["masks"][0]
        if ip.startswith("198.18.") or ip.startswith("198.19.") or ip.startswith("127."):
            continue
        gateways = [g for g in sec["gateways"] if g != "0.0.0.0"]
        if not gateways:
            continue
        name = sec["name"].lower()
        if any(k in name for k in ("vmware", "virtualbox", "hyper-v", "wsl", "vethernet", "loopback")):
            continue
        found.append(NetIf(sec["name"], ip, mask))
    return found


def subnet_hosts(iface: NetIf) -> list[str]:
    ip_i = _ip_int(iface.ip)
    mask_i = _ip_int(iface.mask)
    width = bin(mask_i).count("1")
    if width < 24 or width > 30:
        mask_i = 0xFFFFFF00
    net = ip_i & mask_i
    broadcast = net | (~mask_i & 0xFFFFFFFF)
    hosts = []
    for i in range(net + 1, broadcast):
        if i == ip_i:
            continue
        hosts.append(socket.inet_ntoa(struct.pack("!I", i)))
    return hosts


def same_subnet(ip: str, ifaces: list[NetIf]) -> bool:
    try:
        peer = _ip_int(ip)
    except OSError:
        return False
    for iface in ifaces:
        mask_i = _ip_int(iface.mask)
        if (_ip_int(iface.ip) & mask_i) == (peer & mask_i):
            return True
    return False


def ip_for_peer(peer: str, ifaces: list[NetIf]) -> str:
    try:
        peer_i = _ip_int(peer)
    except OSError:
        peer_i = 0
    best = ""
    best_bits = -1
    for iface in ifaces:
        ip_i = _ip_int(iface.ip)
        mask_i = _ip_int(iface.mask)
        if peer_i and (ip_i & mask_i) == (peer_i & mask_i):
            bits = bin(mask_i).count("1")
            if bits > best_bits:
                best = iface.ip
                best_bits = bits
    if best:
        return best
    if ifaces:
        return ifaces[0].ip
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer, 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _msearch(st: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n"
        "USER-AGENT: Qingying/1.0 UPnP/1.1\r\n"
        "\r\n"
    ).encode("utf-8")


def _parse_ssdp(data: bytes) -> dict[str, str] | None:
    text = data.decode("utf-8", errors="replace")
    head = text.split("\r\n", 1)[0]
    if "200" not in head and "NOTIFY" not in head:
        return None
    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().upper()] = value.strip()
    if not headers.get("LOCATION"):
        return None
    return headers


def _listen(sock: socket.socket, locations: dict[str, str], deadline: float) -> None:
    import time

    while time.time() < deadline:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        headers = _parse_ssdp(data)
        if not headers:
            continue
        loc = headers["LOCATION"]
        locations.setdefault(loc, headers.get("USN", ""))


def _search_interface(iface: NetIf, timeout: float) -> dict[str, str]:
    import time

    locations: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((iface.ip, 0))
        sock.settimeout(0.2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface.ip))
        except OSError:
            pass
        hosts = subnet_hosts(iface)
        started = time.time()

        def blast() -> None:
            for st in SEARCH_TARGETS:
                packet = _msearch(st)
                try:
                    sock.sendto(packet, ("239.255.255.250", 1900))
                except OSError:
                    pass
                # 有线电脑和 Wi-Fi 投影仪之间，组播经常被光猫丢掉，所以再按地址单播一次。
                if st not in (SEARCH_TARGETS[0], "upnp:rootdevice"):
                    continue
                for host in hosts:
                    try:
                        sock.sendto(packet, (host, 1900))
                    except OSError:
                        continue

        blast()
        _listen(sock, locations, time.time() + 0.8)
        blast()
        _listen(sock, locations, time.time() + 1.6)
    finally:
        sock.close()
    return locations


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _join_control(location: str, url_base: str, control: str) -> str:
    control = (control or "").strip()
    if not control:
        return ""
    if control.lower().startswith("http"):
        return control
    parsed = urlparse(location)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if control.startswith("/"):
        return origin + control
    base = url_base or location
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, control)


def fetch_device(location: str) -> Device | None:
    req = urllib.request.Request(location, headers={"User-Agent": "Qingying/1.0"})
    try:
        with urlopen_direct(req, timeout=2.2) as resp:
            raw = resp.read(512_000)
    except Exception:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "AVTransport" not in text and "avtransport" not in text.lower():
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            return None
    for elem in root.iter():
        elem.tag = _local(elem.tag)
    parent = {child: node for node in root.iter() for child in list(node)}
    url_base = ""
    for elem in root.iter("URLBase"):
        url_base = (elem.text or "").strip()
        break

    for svc in root.iter("service"):
        service_type = (svc.findtext("serviceType") or "")
        if "AVTransport" not in service_type:
            continue
        control = _join_control(location, url_base, svc.findtext("controlURL") or "")
        if not control:
            continue
        name = model = manufacturer = udn = ""
        node = svc
        while node is not None:
            if node.tag == "device":
                name = html.unescape((node.findtext("friendlyName") or "").strip())
                model = html.unescape((node.findtext("modelName") or "").strip())
                manufacturer = html.unescape((node.findtext("manufacturer") or "").strip())
                udn = (node.findtext("UDN") or "").strip()
                break
            node = parent.get(node)
        host = urlparse(location).hostname or ""
        if not name:
            name = host or "投影仪"
        low = f"{name} {manufacturer} {model}".lower()
        if "windows media player" in low or manufacturer.lower() == "microsoft corporation":
            return None
        return Device(name, host, model, manufacturer, location, control, udn)
    return None


def _collect(locations: dict[str, str]) -> list[Device]:
    devices: list[Device] = []
    seen: set[str] = set()
    if not locations:
        return devices
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_device, loc) for loc in locations]
        for fut in as_completed(futures):
            device = fut.result()
            if device is None:
                continue
            key = device.udn or device.control_url
            if key in seen:
                continue
            seen.add(key)
            devices.append(device)
    devices.sort(key=lambda d: (not d.is_xiaomi, d.name.lower()))
    return devices


def discover(ifaces: list[NetIf] | None = None, timeout: float = 3.2) -> list[Device]:
    ifaces = ifaces if ifaces is not None else local_interfaces()
    locations: dict[str, str] = {}
    if not ifaces:
        return []
    threads = []
    bags: list[dict[str, str]] = []

    def run(iface: NetIf, bag: dict[str, str]) -> None:
        bag.update(_search_interface(iface, timeout))

    for iface in ifaces:
        bag: dict[str, str] = {}
        bags.append(bag)
        thread = threading.Thread(target=run, args=(iface, bag), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout + 1.5)
    for bag in bags:
        locations.update(bag)
    own = {iface.ip for iface in ifaces}
    locations = {loc: usn for loc, usn in locations.items() if urlparse(loc).hostname not in own}
    return _collect(locations)


def probe_host(ip: str) -> Device | None:
    """按 IP 单播搜索；没有 SSDP 时再试常见的描述端口。"""
    locations: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(0.3)
        try:
            sock.bind(("", 0))
        except OSError:
            pass
        import time

        deadline = time.time() + 1.6
        for st in SEARCH_TARGETS:
            try:
                sock.sendto(_msearch(st), (ip, 1900))
            except OSError:
                break
        _listen(sock, locations, deadline)
    finally:
        sock.close()
    for loc in list(locations):
        if urlparse(loc).hostname not in (ip, None):
            locations.pop(loc, None)
    found = _collect(locations)
    for device in found:
        if device.host == ip:
            return device
    if found:
        return found[0]

    ports = (49152, 49153, 49154, 49155, 8200, 7676, 9092, 38400, 2869, 60000, 60006)
    paths = ("/description.xml", "/rootDesc.xml", "/dmr.xml", "/")

    def hit(port: int, path: str) -> Device | None:
        return fetch_device(f"http://{ip}:{port}{path}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(hit, port, path) for port in ports for path in paths]
        for fut in as_completed(futures):
            device = fut.result()
            if device is not None:
                return device
    return None


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _tag(xml: str, name: str) -> str:
    match = re.search(
        rf"<(?:[\w.-]+:)?{name}[^>]*>(.*?)</(?:[\w.-]+:)?{name}>",
        xml,
        re.I | re.S,
    )
    if not match:
        return ""
    return html.unescape(match.group(1).strip())


def format_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_clock(value: str) -> float | None:
    value = (value or "").strip()
    if not value or value.upper() == "NOT_IMPLEMENTED":
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


class DlnaControl:
    def __init__(self, control_url: str):
        self.control_url = control_url

    def _call(self, action: str, inner: str, timeout: float = 4) -> str:
        service = "urn:schemas-upnp-org:service:AVTransport:1"
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="{service}">'
            f"{inner}"
            f"</u:{action}>"
            "</s:Body></s:Envelope>"
        )
        req = urllib.request.Request(
            self.control_url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
                "User-Agent": "Qingying/1.0",
                "Connection": "close",
            },
            method="POST",
        )
        try:
            with urlopen_direct(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = _tag(detail, "errorDescription") or _tag(detail, "faultstring")
            raise DlnaError(message or f"投影仪返回 HTTP {exc.code}") from exc
        except Exception as exc:
            raise DlnaError(f"无法联系投影仪：{exc}") from exc

    def set_uri(
        self,
        url: str,
        mime: str,
        title: str,
        kind: str = "video",
        duration: float = 0,
        seekable: bool = False,
    ) -> None:
        upnp_class = "object.item.videoItem" if kind == "video" else "object.item.imageItem.photo"
        info = "*"
        if seekable:
            info = "DLNA.ORG_OP=10;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000"
        duration_attr = ""
        if duration and float(duration) > 1:
            duration_attr = f' duration="{format_clock(float(duration))}"'
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{_xml_escape(title)}</dc:title>"
            f"<upnp:class>{upnp_class}</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:{info}"{duration_attr}>{_xml_escape(url)}</res>'
            "</item></DIDL-Lite>"
        )
        inner = (
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>{_xml_escape(url)}</CurrentURI>"
            f"<CurrentURIMetaData>{_xml_escape(didl)}</CurrentURIMetaData>"
        )
        try:
            self._call("SetAVTransportURI", inner)
        except DlnaError:
            inner = (
                "<InstanceID>0</InstanceID>"
                f"<CurrentURI>{_xml_escape(url)}</CurrentURI>"
                "<CurrentURIMetaData></CurrentURIMetaData>"
            )
            self._call("SetAVTransportURI", inner)

    def play(self) -> None:
        self._call("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")

    def seek(self, seconds: float) -> None:
        target = format_clock(seconds)
        self._call(
            "Seek",
            f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{target}</Target>",
        )

    def pause(self) -> None:
        self._call("Pause", "<InstanceID>0</InstanceID>")

    def stop(self) -> None:
        self._call("Stop", "<InstanceID>0</InstanceID>")

    def transport_state(self) -> str:
        xml = self._call("GetTransportInfo", "<InstanceID>0</InstanceID>")
        return _tag(xml, "CurrentTransportState") or "UNKNOWN"

    def position(self) -> tuple[float | None, float | None]:
        xml = self._call("GetPositionInfo", "<InstanceID>0</InstanceID>")
        return parse_clock(_tag(xml, "RelTime")), parse_clock(_tag(xml, "TrackDuration"))
