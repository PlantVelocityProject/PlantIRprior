#!/usr/bin/env python3
"""Submit explicit local task requests to the project owner's Unix service.

This client deliberately never retries an RPC: a missing reply does not prove
that a start or resume request was not accepted. Reuse the same request ID when
explicitly checking or resubmitting an uncertain request.
"""
import argparse
import json
import math
from pathlib import Path
import pwd
import socket
import struct
import sys
import time

DEFAULT_SOCKET = Path(__file__).resolve().parents[1] / 'state/agent/service.sock'
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ClientError(Exception):
    """A request could not be delivered or its reply could not be trusted."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('invalid JSON constant: ' + value)


def _check_reply_values(reply):
    # Keep replies safe to serialize on supported Python versions, including 3.8.
    pending = [(reply, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 128:
            raise ClientError('server reply is nested too deeply')
        if isinstance(value, float) and not math.isfinite(value):
            raise ClientError('server reply contains a nonfinite number')
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def rpc(path, request, expected_uid=None, timeout=300):
    """Send exactly one request; return a validated reply, including errors.

    expected_uid is injectable for isolated servers; real callers always check
    the owner account. The deadline covers the entire connection, not each read.
    """
    try:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ClientError('timeout must be finite and positive')
        if not isinstance(request, dict):
            raise ClientError('request must be a JSON object')
        wire = (json.dumps(request, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(wire) > MAX_REQUEST_BYTES:
            raise ClientError('request exceeds size limit of 65536 bytes')
        if expected_uid is None:
            expected_uid = pwd.getpwnam('Zhuxiangrong').pw_uid
        deadline = time.monotonic() + timeout

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ClientError('request timed out; acceptance may be uncertain')
            return seconds

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            connection.connect(str(path))
            if not hasattr(socket, 'SO_PEERCRED'):
                raise ClientError('server UID verification is unavailable')
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            _, uid, _ = struct.unpack('3i', credentials)
            if uid != expected_uid:
                raise ClientError('server UID does not match the project owner')
            connection.settimeout(remaining())
            connection.sendall(wire)
            response = bytearray()
            while True:
                connection.settimeout(remaining())
                block = connection.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
                if not block:
                    raise ClientError('server reply ended without a newline')
                response.extend(block)
                if len(response) > MAX_RESPONSE_BYTES:
                    raise ClientError('server reply exceeds size limit of 4 MiB')
                newline = response.find(b'\n')
                if newline >= 0:
                    if newline != len(response) - 1:
                        raise ClientError('unexpected data after server reply')
                    break
            reply = json.loads(response[:-1].decode('utf-8'), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
            _check_reply_values(reply)
            if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
                raise ClientError('server reply must be an object with a boolean ok field')
            if not reply['ok'] and not isinstance(reply.get('error'), str):
                raise ClientError('server error reply must include an error string')
            return reply
    except ClientError:
        raise
    except socket.timeout as exc:
        raise ClientError('request timed out; acceptance may be uncertain') from exc
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ClientError(str(exc)) from exc


def _bounded_integer(lower, upper):
    def parse(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('must be an integer') from exc
        if not lower <= number <= upper:
            raise argparse.ArgumentTypeError('must be between {} and {}'.format(lower, upper))
        return number
    return parse


def _positive_timeout(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a number') from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def _transport_options(target, defaults=True):
    target.add_argument('--socket', type=Path,
                        default=DEFAULT_SOCKET if defaults else argparse.SUPPRESS,
                        help='local service socket')
    target.add_argument('--timeout', type=_positive_timeout,
                        default=300 if defaults else argparse.SUPPRESS,
                        help='total request timeout in seconds (default: 300)')


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _transport_options(result)
    commands = result.add_subparsers(dest='op', required=True)
    for name in ('ping', 'plan', 'start', 'status', 'resume', 'deliver'):
        command = commands.add_parser(name, allow_abbrev=False)
        _transport_options(command, defaults=False)
        if name in ('plan', 'start'):
            command.add_argument('--species', action='append', required=True,
                                 choices=('ath', 'gmx', 'osa', 'sly', 'zma'))
            command.add_argument('--max-samples', type=_bounded_integer(1, 10), default=10)
            command.add_argument('--discovery-limit', type=_bounded_integer(1, 100), default=100)
            command.add_argument('--skip-discovery', action='store_true')
            command.add_argument('--publish-only',action='store_true',help='publish existing evidence and deliver, with zero sample attempts')
            command.add_argument('--discovery-source', choices=('auto', 'ncbi', 'ena', 'ddbj', 'ngdc'), default='auto')
            command.add_argument('--download-backend', choices=('auto', 'kingfisher', 'qtransc', 'ena-https'), default='auto')
            command.add_argument('--download-timeout', type=_bounded_integer(1, 7200), default=900)
            command.add_argument('--download-retries', type=_bounded_integer(0, 3), default=2)
            command.add_argument('--accession', action='append', default=[])
        if name=='deliver':
            source=command.add_mutually_exclusive_group(required=True)
            source.add_argument('--source-task')
            source.add_argument('--species',action='append',choices=('ath','gmx','osa','sly','zma'))
            command.add_argument('--target',action='append',required=True,choices=('resources','website'))
        if name in ('start', 'resume', 'deliver'):
            command.add_argument('--request-id', required=True)
        if name in ('status', 'resume'):
            command.add_argument('--task-id', required=True)
        if name == 'status':
            command.add_argument('--tail', type=_bounded_integer(0, 8192), default=0)
    return result


def build_request(args):
    request = {'op': args.op}
    if args.op in ('plan', 'start'):
        request['params'] = {name: getattr(args, name) for name in (
            'species', 'max_samples', 'discovery_limit', 'skip_discovery', 'discovery_source',
            'download_backend', 'download_timeout', 'download_retries', 'accession')}
        if args.publish_only:request['params']['publish_only']=True
    if args.op=='deliver':
        request['params']=dict(source_task=args.source_task,species=args.species or [],targets=args.target)
    if args.op in ('start', 'resume', 'deliver'):
        request['request_id'] = args.request_id
    if args.op in ('status', 'resume'):
        request['task_id'] = args.task_id
    if args.op == 'status':
        request['tail'] = args.tail
    return request


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        reply = rpc(args.socket, build_request(args), timeout=args.timeout)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        reply = {'ok': False, 'error': 'invalid arguments; see stderr'}
    except ClientError as exc:
        reply = {'ok': False, 'error': str(exc)}
    print(json.dumps(reply, ensure_ascii=True, allow_nan=False))
    return 0 if reply['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
