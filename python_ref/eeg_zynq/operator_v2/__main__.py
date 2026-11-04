from pathlib import Path
import argparse
import json
import time
from .runtime import BUILD,RUNS,read_json,atomic_json,Manager,ssh_command


def main():
    p=argparse.ArgumentParser(description='P5B operator candidate; GUI starts static/disconnected.')
    target_commands={'targetstatus':['status'],'targetstop':['stop'],'targetlogs':['logs'],
        'displaystart':['display','start'],'displaystop':['display','stop'],'displaystatus':['display','status'],
        'stimulusstart':['stimulus','start'],'stimulusstop':['stimulus','stop']}
    p.add_argument('command',nargs='?',default='gui',choices=['gui','start','status','stop','logs','check',*target_commands])
    p.add_argument('--recording',choices=['off','segments'],default='off')
    a=p.parse_args()
    if a.command in target_commands:
        print(ssh_command(['eegctl',*target_commands[a.command]]));return 0
    if a.command=='gui':
        from .qt import run
        return run()
    if a.command=='start':
        manager=Manager()
        try:
            session=manager.start(a.recording)
            try:
                while not session.done.is_set():
                    session.touch_ui();manager.poll();time.sleep(.1)
            except KeyboardInterrupt:
                session.stop('OPERATOR_CTRL_C')
                while not session.done.wait(.1):session.touch_ui()
            manager.poll();print(json.dumps(session.snapshot(False),ensure_ascii=False,indent=2))
            return 0 if session.clean else 4
        finally:manager.close()
    if a.command=='status':
        print(json.dumps(read_json(RUNS/'latest.json',{'state':'NO_LOCAL_RUN','deployed':False}),ensure_ascii=False,indent=2));return 0
    if a.command=='logs':
        folders=sorted(RUNS.glob('run-*')) if RUNS.exists() else []
        print('\n'.join(str(p) for p in folders[-20:]) or str(RUNS));return 0
    if a.command=='stop':
        active=read_json(RUNS/'active.json')
        if not active:print('NO_ACTIVE_LOCAL_OWNER; target status not inferred');return 0
        folder=Path(active['folder']).resolve()
        if folder.parent!=RUNS.resolve():raise RuntimeError('RUN_FOLDER_OUTSIDE_WORKSPACE')
        atomic_json(RUNS/'stop.request.json',dict(run_id=active['run_id']))
        print('STOP_REQUESTED; inspect Status for target END/GPIO confirmation');return 0
    from . import ssvep
    from PySide6 import __version__
    print(json.dumps(dict(version='p5b-operator-v2',PySide6=__version__,local_only=True,
        typing_languages=['EN','ZH_PINYIN'],letters=26,wire_operator_version=1,
        deployment=read_json(BUILD/'deployment.json',None),channels=list(ssvep.CHANNELS),
        default_stimulus='OFF',default_acquisition='OFF',target_raw='NO_GROWING_FILE'),ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
