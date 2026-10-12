#!/usr/bin/env python3
"""Check publishable Git contents without printing the potentially private values.

The default scans tracked and non-ignored untracked working files. --staged reads
the Git index instead (including unchanged files), never unstaged replacements.
This is a heuristic guard, not a secret scanner or a review of images/history.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import urlsplit


MAX_BYTES = 8 * 1024 * 1024
# Network definitions, not endpoints of a developer's network.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',  # privacy: allow[private-ip] standard RFC 1918 ranges
    '169.254.0.0/16', '100.64.0.0/10', 'fc00::/7', 'fe80::/10',  # privacy: allow[private-ip] standard local ranges
))
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.]*(?:%[\w.-]+)?')
HOME_PATH = re.compile(r'/(?:home|Users)/([^/\s\"\'<>`{}$)\\:;]+)')
WINDOWS_HOME = re.compile(r'\b[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"\'<>`{}$)]+)', re.I)
PLACEHOLDER_USERS = frozenset(('user', 'username', 'example', 'test', 'tester', 'u', 'runner',
                               'nazwa-uzytkownika', 'uzytkownik', 'użytkownik', 'shared', 'public'))
URL = re.compile(r'\b(?:https?|ssh|sftp|ftp|wss?)://[^\s<>\"\'`]+', re.I)
LOCAL_HOST = re.compile(r'(?<![\w.-])(?:[a-zA-Z0-9-]+\.)+(?:lan|internal|home\.arpa)(?![\w.-])', re.I)
HOST_SETTING = re.compile(r'\b(?:host(?:name)?|server|endpoint)\s*[:=]\s*[\"\']?([\w.-]+)', re.I)
SECRET_PATTERNS = (
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b'),
)
SECRET_ASSIGNMENT = re.compile(
    r'''["']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)\b["']?'''
    r'''\s*[:=]\s*["']([A-Za-z0-9_./+=:@!$%&*?-]{16,})["']''', re.I)
ALLOW = re.compile(r'privacy: allow\[([a-z,-]+)\]\s+([^\r\n]+)')
ALLOWABLE = frozenset(('private-ip', 'home-path', 'local-host'))
RULES = {
    'private-ip': 'private or link-local network address; use a documentation address or a variable',
    'home-path': 'personal home directory; use ~, $HOME or a neutral placeholder',
    'local-host': 'local host name; use localhost, example.com or a configuration variable',
    'secret': 'possible credential or private key; remove it and check whether rotation is needed',
    'credential-file': 'credential/configuration file name; keep real credentials outside Git',
    'private-artifact': 'local-only investigation directory; publish a reviewed synthetic example instead',
    'too-large': 'file exceeds the 8 MiB scan limit; review and store large assets outside Git',
    'unreadable': 'cannot read this Git entry; fix the file or index before publishing',
    'submodule': 'submodule contents are not scanned; audit and publish the dependency separately',
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def private_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in PRIVATE_NETWORKS)


def local_host(host: str) -> bool:
    host = host.lower().rstrip('.')
    if host == 'localhost' or host.endswith('.localhost'):
        return False
    return host.endswith(('.local', '.lan', '.internal', '.home', '.home.arpa'))


def placeholder(value: str) -> bool:
    value = value.strip()
    return (value.startswith(('$', '<', '${', 'os.environ', 'process.env'))
            or re.fullmatch(r'(?:example|test|dummy|placeholder|replace[-_]?me|your[-_][\w-]+)(?:[-_][\w-]+)?', value, re.I)
            is not None)


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        rules = set()
        if any(private_ip(match.group()) for pattern in (IPV4, IPV6) for match in pattern.finditer(line)):
            rules.add('private-ip')
        if any(match.group(1).lower() not in PLACEHOLDER_USERS
               for pattern in (HOME_PATH, WINDOWS_HOME) for match in pattern.finditer(line)):
            rules.add('home-path')
        if LOCAL_HOST.search(line):
            rules.add('local-host')
        for match in URL.finditer(line):
            try:
                parsed = urlsplit(match.group().rstrip('.,);]'))
                if parsed.hostname and local_host(parsed.hostname):
                    rules.add('local-host')
            except ValueError:
                pass  # malformed URL fixtures are not a hostname
        # A .local bundle identifier is not a hostname. Only recognise it in
        # network contexts, leaving org.example.local and imports alone.
        if any(local_host(match.group(1)) for match in HOST_SETTING.finditer(line)):
            rules.add('local-host')
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            rules.add('secret')
        if any(not placeholder(match.group(1)) for match in SECRET_ASSIGNMENT.finditer(line)):
            rules.add('secret')
        for allow in ALLOW.finditer(line):
            # A reason and named rule are required; no wildcard or secret bypass.
            if len(allow.group(2).strip()) >= 12:
                rules.difference_update(set(allow.group(1).split(',')) & ALLOWABLE)
        findings.extend(Finding(path, number, rule) for rule in sorted(rules))
    return findings


def scan_bytes(path: str, data: bytes) -> list[Finding]:
    if len(data) > MAX_BYTES:
        return [Finding(path, 1, 'too-large')]
    # Scan printable strings in binaries too (for example image metadata), but
    # never claim this reads text visible in screenshots or compressed payloads.
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16', errors='replace')
    else:
        text = data.decode('utf-8-sig', errors='replace')
    findings = scan_text(path, text)
    if ('local-private' in PurePosixPath(path).parts
            or path.startswith(('linux/research/', 'bench/lab/'))):
        findings.append(Finding(path, 1, 'private-artifact'))
    name = PurePosixPath(path).name.lower()
    if (name in ('.env', 'auth.json', 'id_rsa', 'id_ed25519', 'id_ecdsa')
            or name.startswith('.env.') and name not in ('.env.example', '.env.sample', '.env.template')
            or path.endswith('/.aws/credentials') or path == '.aws/credentials'
            or name.endswith(('.p12', '.pfx'))):
        findings.append(Finding(path, 1, 'credential-file'))
    return findings


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Git could not enumerate/read repository files')
    return result.stdout


def entries(root: Path, staged: bool, tracked_only: bool):
    """Yield path, mode, blob ID; unmerged index entries fail closed."""
    indexed = {}
    for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not entry:
            continue
        info, name = entry.split(b'\t', 1)
        mode, oid, stage = info.decode('ascii').split()
        path = os.fsdecode(name)
        indexed[path] = (mode if stage == '0' else 'unmerged', oid)
    if not staged and not tracked_only:
        for name in git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if name:
                indexed.setdefault(os.fsdecode(name), ('working', None))
    for path, (mode, oid) in sorted(indexed.items()):
        yield path, mode, oid


def scan_repository(root: Path, staged: bool = False, tracked_only: bool = False) -> tuple[list[Finding], int]:
    findings, count = [], 0
    for path, mode, oid in entries(root, staged, tracked_only):
        if mode == '160000':
            findings.append(Finding(path, 1, 'submodule'))
            continue
        if mode == 'unmerged':
            findings.append(Finding(path, 1, 'unreadable'))
            continue
        try:
            if staged:
                size = int(git(root, 'cat-file', '-s', oid))
                data = git(root, 'cat-file', 'blob', oid) if size <= MAX_BYTES else b''
            else:
                file = root / path
                if not file.parent.resolve().is_relative_to(root.resolve()):
                    raise OSError('parent directory links outside repository')
                if file.is_symlink():
                    # Inspect the link itself; never follow it outside the repo.
                    data = os.readlink(file).encode('utf-8', errors='surrogateescape')
                    size = len(data)
                else:
                    try:
                        size = file.stat().st_size
                    except FileNotFoundError:
                        if mode != 'working':
                            continue  # a deletion in the working tree
                        raise
                    if size <= MAX_BYTES:
                        with file.open('rb') as source:
                            data = source.read(MAX_BYTES + 1)
                    else:
                        data = b''
            count += 1
            findings.extend(scan_text(path, path))
            findings.extend([Finding(path, 1, 'too-large')] if size > MAX_BYTES else scan_bytes(path, data))
        except (OSError, RuntimeError, ValueError):
            findings.append(Finding(path, 1, 'unreadable'))
    return sorted(set(findings), key=lambda found: (found.path, found.line, found.rule)), count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='scan index contents, for pre-commit use')
    parser.add_argument('--tracked-only', action='store_true', help='omit untracked files in the working tree')
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(git(Path.cwd(), 'rev-parse', '--show-toplevel')).strip())
        findings, count = scan_repository(root, args.staged, args.tracked_only)
    except (OSError, RuntimeError) as exc:
        print('Privacy check failed: %s' % type(exc).__name__, file=sys.stderr)
        return 2
    for finding in findings:
        print('%s:%d: [%s] %s' % (finding.path, finding.line, finding.rule, RULES[finding.rule]))
    print('Privacy check: %d file(s), %d finding(s). Values are not printed.' % (count, len(findings)))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
