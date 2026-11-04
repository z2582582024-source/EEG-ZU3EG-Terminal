"""Safe default UI, explicit simulated/replay paths; live requires bound authority."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from .protocol import SIMULATED
from .stream import BUILD, PROJECT, NetworkSession, ReplaySession, SimulatedPeer, inside_project


def live_scope(path):
    # Full cold controller owns COM/TCF and releases NET1 only at current READY.
    # This path cannot issue a permit, replay a closed event, or skip checkpoints.
    from verification.p5b_continuous_cap_v1.host.launcher import execute_with_qt
    session=inside_project(path)
    result=execute_with_qt(session,session.with_name('permit.json'))
    print(result['result'])
    return 0 if result['result']=='CONT1_NETWORK_QT_OBSERVATION_SIGNAL_QUALITY_SEPARATE' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--approval", type=Path, help="Issued CONT1 session.json with adjacent matched permit.json")
    parser.add_argument("--stop", type=Path, help="Project run directory; local stop request only")
    parser.add_argument("--seconds", type=int, default=0, help="0: manual stop with watchdog/resource protection; live uses approved scope")
    parser.add_argument("--self-check", action="store_true", help="Loopback simulated stream, no hardware")
    args = parser.parse_args()
    if sum(bool(x) for x in (args.simulate, args.replay, args.approval, args.stop, args.self_check)) > 1:
        parser.error("Choose one source/action")
    if args.stop:
        folder = inside_project(args.stop)
        with (folder / "stop.request").open("x", encoding="utf-8") as f: f.write("USER_STOP_COMMAND\n")
        print("Local stop request saved; verify target shutdown in manifest. No reconnect or target command issued.")
        return 0
    if args.approval:
        return live_scope(args.approval)
    session, peer = None, None
    if args.simulate or args.self_check:
        tag = uuid.uuid4().bytes
        peer = SimulatedPeer(tag, seconds=args.seconds)
        session = NetworkSession("127.0.0.1", peer.port, tag, mode=SIMULATED, seconds=args.seconds)
    elif args.replay:
        session = ReplaySession(args.replay)
    if args.self_check:
        session.connect(); end = time.monotonic()+args.seconds+6
        while not session.done.is_set() and time.monotonic() < end:
            session.ui_tick = time.monotonic()
            if session.state == "READY" and not session.start_request.is_set(): session.start()
            time.sleep(.02)
        if not session.done.is_set(): session.stop("SELF_CHECK_TIMEOUT")
        state = session.snapshot(); state.pop("samples"); print(json.dumps(state, ensure_ascii=False))
        return 0 if session.done.is_set() and not session.failure and session.frames > 256 else 1
    from .qt import run
    return run(session, peer)


if __name__ == "__main__":
    raise SystemExit(main())
