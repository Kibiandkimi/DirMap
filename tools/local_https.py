#!/usr/bin/env python3
"""Local HTTPS for phone testing, without modifying the system trust store.

The optional HTTP listener serves only the public CA certificate. Private
keys and the teacher token stay outside the project in a private directory.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(*arguments):
    subprocess.run(['openssl', *map(str, arguments)], check=True, capture_output=True)


def local_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('1.1.1.1', 80))  # routing lookup; no packet is sent
            return probe.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def prepare(directory, names):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca, ca_key = directory/'ca.crt', directory/'ca-key.pem'
    leaf, key = directory/'server.crt', directory/'server-key.pem'
    if ca.exists() != ca_key.exists():
        raise ValueError('本地 CA 证书和私钥不完整，请恢复备份或指定新的证书目录')
    sans = []
    for name in sorted(set(['localhost', '127.0.0.1', '::1', *names])):
        try:
            sans.append('IP:'+str(ipaddress.ip_address(name)))
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', name):
                raise ValueError('HTTPS 主机名无效')
            sans.append('DNS:'+name)
    if not ca.exists():
        run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', ca_key, '-out', ca,
            '-days', '3650', '-sha256', '-subj', '/CN=DirMap Local CA',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    specification = ','.join(sans)
    saved = directory/'names.json'
    current = saved.read_text() if saved.exists() else None
    valid = leaf.exists() and key.exists() and subprocess.run(['openssl', 'x509', '-checkend', '86400', '-noout', '-in', str(leaf)], capture_output=True).returncode == 0
    if not valid or current != json.dumps(specification):
        config = directory/'extensions.cnf'
        config.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName='+specification+'\n')
        request = directory/'server.csr'
        run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', request, '-subj', '/CN=DirMap Local')
        run('x509', '-req', '-in', request, '-CA', ca, '-CAkey', ca_key, '-CAcreateserial', '-out', leaf,
            '-days', '365', '-sha256', '-extfile', config)
        saved.write_text(json.dumps(specification))
        request.unlink(missing_ok=True)
    for private in (ca_key, key):
        private.chmod(0o600)
    token_path = directory/'teacher-token'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(24))
        token_path.chmod(0o600)
    return ca, leaf, key, token_path


def certificate_server(host, port, certificate):
    class PublicCertificate(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/ca.crt':
                self.send_error(404)
                return
            data = certificate.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-x509-ca-cert')
            self.send_header('Content-Disposition', 'attachment; filename="dirmap-local-ca.crt"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass
    return ThreadingHTTPServer((host, port), PublicCertificate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--name', action='append', default=[], help='Additional IP or DNS name in the server certificate')
    parser.add_argument('--directory', type=Path, default=Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))/'dirmap/tls')
    parser.add_argument('--serve-ca', action='store_true', help='Serve ONLY the public CA at HTTP port + 1')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port < 65535:
        parser.error('port must be between 1 and 65534')
    address = local_address() if args.host in ('0.0.0.0', '::') else args.host
    os.umask(0o077)
    ca, cert, key, token = prepare(args.directory, [address, *args.name])
    os.environ.setdefault('DIRMAP_ADMIN_TOKEN', token.read_text().strip())
    url_address = f'[{address}]' if ':' in address else address
    print(f'HTTPS: https://{url_address}:{args.port}', flush=True)
    print(f'Public CA: {ca}', flush=True)
    print(f'Teacher token file: {token}', flush=True)
    if args.prepare_only:
        return
    http = None
    try:
        if args.serve_ca:
            http = certificate_server(args.host, args.port+1, ca)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            print(f'Phone CA download: http://{url_address}:{args.port+1}/ca.crt', flush=True)
        import uvicorn
        uvicorn.run('dirmap.app:app', host=args.host, port=args.port, ssl_certfile=str(cert), ssl_keyfile=str(key))
    finally:
        if http:
            http.shutdown(); http.server_close()


if __name__ == '__main__':
    main()
