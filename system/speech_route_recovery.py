#!/usr/bin/python3
"""Explicit route rollback/restore; model and evidence remain retained."""
from pathlib import Path
import fcntl,hashlib,json,os,subprocess,sys,uuid
work=Path('/opt/eeg/workspace/chinese-tts-20261024-01')
release=Path('/opt/eeg/tts/releases/zh-piper-20261024-01')
target=Path('/usr/bin/eeg-speak')
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):
    with p.open('x',encoding='utf-8') as f:json.dump(v,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
def main():
    plan=json.loads((release/'installed_manifest.json').read_text(encoding='utf-8'))
    current=digest(target);old=work/'install01/eeg-speak.original';new=release/'eeg_speech.py'
    assert not target.is_symlink() and digest(old)==plan['before_sha256'] and digest(new)==plan['after_sha256']
    assert current in (plan['before_sha256'],plan['after_sha256']),'UNRECOGNIZED_ROUTE_NO_WRITE'
    action=sys.argv[1] if len(sys.argv)>1 else 'status'
    status={'route_sha256':current,'route':'original_espeak_both_languages' if current==plan['before_sha256'] else 'new_chinese_original_english',
      'rollback_sha256':plan['before_sha256'],'restore_sha256':plan['after_sha256'],'automatic_playback':False}
    if action in ('status','plan'):print(json.dumps(status));return
    assert len(sys.argv)==3 and action in ('rollback','restore') and sys.argv[2]==current,'EXACT_CURRENT_SHA256_REQUIRED'
    wanted=plan['before_sha256'] if action=='rollback' else plan['after_sha256']
    assert wanted!=current,'ALREADY_SELECTED_NO_WRITE'
    assert digest(Path('/etc/systemd/system/eeg-chinese-tts.service'))==plan['unit_sha256']
    fd=os.open('/run/eeg-speech.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        job=work/'route-recovery'/uuid.uuid4().hex;job.mkdir(parents=True,mode=0o700)
        write(job/'started.json',{'completion':'UNKNOWN','action':action,'before_sha256':current,'after_sha256':wanted})
        subprocess.run(['systemctl','stop','eeg-chinese-tts'],check=True,timeout=20)
        source=old if action=='rollback' else new
        pending=target.with_name('.eeg-speak.route-recovery.pending');assert not pending.exists()
        data=source.read_bytes();assert hashlib.sha256(data).hexdigest()==wanted
        with pending.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        pending.chmod(0o755);assert digest(target)==current;os.replace(pending,target)
        directory=os.open(str(target.parent),os.O_RDONLY|os.O_DIRECTORY);os.fsync(directory);os.close(directory)
        subprocess.run(['systemctl','disable' if action=='rollback' else 'enable','eeg-chinese-tts'],check=True,timeout=20)
        if action=='restore':subprocess.run(['systemctl','start','eeg-chinese-tts'],check=True,timeout=20)
        assert digest(target)==wanted
        write(job/'result.json',{'completed':True,'action':action,'sha256':wanted,'automatic_playback':False})
        print((job/'result.json').read_text(encoding='utf-8'))
    finally:os.close(fd)
if __name__=='__main__':
    try:main()
    except Exception as e:print('SPEECH_ROUTE_RECOVERY_ERROR '+str(e),file=sys.stderr);sys.exit(1)
