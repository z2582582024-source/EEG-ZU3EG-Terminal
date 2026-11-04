#!/usr/bin/env python3
"""Apply one udhcpc lease without accepting settings from another interface."""
import ipaddress
import os
from pathlib import Path
import subprocess
import sys


def command(*args):
    subprocess.run(args, check=True, timeout=10)


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ''
    interface = os.environ.get('interface', '')
    if not interface or not (Path('/sys/class/net') / interface / 'device').exists():
        return 3
    device = (Path('/sys/class/net') / interface / 'device').resolve().name
    if device != 'ff0e0000.ethernet':
        return 3
    if event in ('bound', 'renew'):
        address = ipaddress.IPv4Address(os.environ['ip'])
        netmask = ipaddress.IPv4Address(os.environ.get('subnet', '255.255.255.0'))
        prefix = sum(bin(part).count('1') for part in netmask.packed)
        if int(netmask) != int(ipaddress.IPv4Network('0.0.0.0/' + str(prefix)).netmask):
            return 3
        command('ip', '-4', 'address', 'flush', 'dev', interface, 'scope', 'global')
        command('ip', '-4', 'address', 'replace', str(address) + '/' + str(prefix), 'dev', interface)
        gateways = os.environ.get('router', '').split()
        if gateways:
            gateway = ipaddress.IPv4Address(gateways[0])
            command('ip', '-4', 'route', 'replace', 'default', 'via', str(gateway), 'dev', interface)
        dns = os.environ.get('dns', '').split()
        if dns:
            body = ''.join('nameserver ' + str(ipaddress.ip_address(value)) + '\n' for value in dns)
            temporary = Path('/etc/.resolv.conf.eeg-new')
            temporary.write_text(body)
            os.chmod(temporary, 0o644)
            os.replace(str(temporary), '/etc/resolv.conf')
    elif event == 'deconfig':
        command('ip', '-4', 'address', 'flush', 'dev', interface, 'scope', 'global')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(3)
