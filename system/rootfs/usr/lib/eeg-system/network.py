#!/usr/bin/env python3
"""Identify the PS GEM by sysfs, then manage only that interface's IPv4 config."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

CONFIG = Path('/etc/eeg-system/network.json')
DEVICE = 'ff0e0000.ethernet'
PHY = '0x4f51e91b'


def run(args, check=True):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=15)


def validate(config):
    if (config.get('schema') != 'eeg.network.v1' or
            config.get('device_path') != DEVICE or config.get('phy_id') != PHY):
        raise ValueError('PS_ETH_CONFIG_IDENTITY_MISMATCH')
    if config.get('mode') not in ('static', 'dhcp'):
        raise ValueError('STATIC_OR_DHCP_REQUIRED')
    if config['mode'] == 'static':
        address = ipaddress.IPv4Interface(config['address'])
        if config.get('netmask') and config['netmask'] != str(address.network.netmask):
            raise ValueError('ADDRESS_AND_NETMASK_DISAGREE')
        if (address.ip.is_loopback or address.ip.is_multicast or address.ip.is_unspecified or
                address.ip in (address.network.network_address, address.network.broadcast_address)):
            raise ValueError('UNICAST_HOST_ADDRESS_REQUIRED')
        if config.get('gateway'):
            gateway = ipaddress.IPv4Address(config['gateway'])
            if gateway not in address.network or gateway == address.ip:
                raise ValueError('GATEWAY_OUTSIDE_SUBNET_OR_SELF')
    for server in config.get('dns', []):
        ipaddress.ip_address(server)
    return config


def identify(check_phy=True):
    found = []
    for interface in Path('/sys/class/net').iterdir():
        device = interface / 'device'
        if device.exists() and device.resolve().name == DEVICE:
            found.append(interface)
    if len(found) != 1:
        raise RuntimeError('UNIQUE_PS_GEM_NOT_FOUND')
    interface = found[0]
    phy = interface / 'phydev/phy_id'
    if check_phy and (not phy.exists() or phy.read_text().strip().lower() != PHY):
        raise RuntimeError('PS_PHY_IDENTITY_MISMATCH')
    return interface.name


def write_atomic(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply(config):
    interface = identify(check_phy=False)
    run(['ip', 'link', 'set', 'dev', interface, 'up'])
    # macb can attach its PHY only on the first open. Validate after link-up;
    # asking for phydev before opening the interface rejects a correct boot.
    phy = Path('/sys/class/net') / interface / 'phydev/phy_id'
    for attempt in range(20):
        if phy.exists():
            break
        time.sleep(0.1)
    identify(check_phy=True)
    if config['mode'] == 'dhcp':
        run(['ip', '-4', 'address', 'flush', 'dev', interface, 'scope', 'global'])
        # Foreground one-shot: no hidden daemon later overwrites a static choice.
        result = run(['udhcpc', '-n', '-q', '-t', '3', '-T', '3', '-i', interface,
                      '-s', '/usr/lib/eeg-system/udhcpc.script'], check=False)
        if result.returncode:
            raise RuntimeError('DHCP_NO_LEASE: use serial eeg-network temporary ADDRESS/PREFIX')
    else:
        address = str(ipaddress.IPv4Interface(config['address']))
        current = json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', interface]).stdout)
        wanted = ipaddress.IPv4Interface(address)
        present = any(row.get('local') == str(wanted.ip) and row.get('prefixlen') == wanted.network.prefixlen
                      for item in current for row in item.get('addr_info', []))
        carrier = (Path('/sys/class/net') / interface / 'carrier').read_text().strip()
        if not present and carrier == '1':
            probe = run(['arping', '-D', '-I', interface, '-c', '2', '-w', '3', str(wanted.ip)], check=False)
            if probe.returncode:
                raise RuntimeError('IP_CONFLICT_OR_ARP_PROBE_FAILED; address not changed')
        # Never flush the interface before conflict checking the new address.
        run(['ip', '-4', 'address', 'replace', address, 'dev', interface])
        for item in current:
            for row in item.get('addr_info', []):
                if row.get('scope') == 'global' and row.get('local') != str(wanted.ip):
                    run(['ip', '-4', 'address', 'del', row['local'] + '/' + str(row['prefixlen']),
                         'dev', interface])
        if config.get('gateway'):
            run(['ip', '-4', 'route', 'replace', 'default', 'via', config['gateway'], 'dev', interface])
        else:
            run(['ip', '-4', 'route', 'del', 'default', 'dev', interface], check=False)
        # .1 is deliberately not assumed to provide routing or DNS.
        if config.get('dns'):
            write_atomic(Path('/etc/resolv.conf'), ''.join('nameserver ' + x + '\n' for x in config['dns']), 0o644)
    return interface


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('apply')
    for command in ('temporary', 'save'):
        sub = commands.add_parser(command)
        sub.add_argument('address', help='IPv4 address/prefix, or dhcp')
        sub.add_argument('--gateway')
        sub.add_argument('--dns', action='append', default=[])
    args = parser.parse_args()
    if args.command == 'status':
        interface = identify()
        print(json.dumps({'interface': interface, 'device_path': DEVICE,
                          'phy_id': PHY, 'persistent': json.loads(CONFIG.read_text()),
                          'live': json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', interface]).stdout)}, indent=2))
        return 0
    if os.geteuid() != 0:
        raise PermissionError('ROOT_REQUIRED')
    Path('/run/lock').mkdir(parents=True, exist_ok=True)
    with open('/run/lock/eeg-network.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'apply':
            config = validate(json.loads(CONFIG.read_text()))
        else:
            config = validate(dict(schema='eeg.network.v1', device_path=DEVICE, phy_id=PHY,
                                   mode='dhcp' if args.address == 'dhcp' else 'static',
                                   address=None if args.address == 'dhcp' else args.address,
                                   netmask=None if args.address == 'dhcp' else str(ipaddress.IPv4Interface(args.address).network.netmask),
                                   gateway=args.gateway, dns=args.dns))
        interface = apply(config)
        if args.command == 'save':
            write_atomic(CONFIG, json.dumps(config, indent=2) + '\n')
        print('EEG_NETWORK_READY interface=' + interface + ' persistent=' + str(args.command != 'temporary').lower())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('EEG_NETWORK_FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(3)
