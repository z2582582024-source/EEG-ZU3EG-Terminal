#!/usr/bin/python3
"""Explicit, finite speech output and persistent ALSA device selection."""
import argparse
import html
import hashlib
import shutil
from array import array
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave

CONFIG = Path('/etc/eeg/audio.json')
ESPEAK = '/usr/bin/espeak'
APLAY = '/usr/bin/aplay'
DATA_PARENT = '/usr/share'
CHINESE_CONFIG = Path('/etc/eeg/chinese_tts.json')
ENGLISH_CONFIG = Path('/etc/eeg/english_tts.json')
VOLUME_FILE=Path('/run/eeg-speech/volume.json')
AMIXER='/usr/bin/amixer'
WELCOME_WAV=Path('/opt/eeg/share/speech/welcome-en-lessac-20261025-01.wav')
WELCOME_SHA256='6e80b2627d1762c558ffca9c4683502f854ffc465c86774e4c17e1efcebc24a5'

def english_welcome(text,amplitude,filename):
    """Operator-selected fixed welcome, generated once; other English keeps eSpeak."""
    if text.strip().rstrip('.').casefold()!='welcome to the eeg acquisition system':return None
    if not WELCOME_WAV.is_file() or WELCOME_WAV.is_symlink() or hashlib.sha256(WELCOME_WAV.read_bytes()).hexdigest()!=WELCOME_SHA256:
        raise RuntimeError('WELCOME_AUDIO_MISSING_OR_SHA_MISMATCH')
    with wave.open(str(WELCOME_WAV),'rb') as source:
        if source.getnchannels()!=1 or source.getsampwidth()!=2 or source.getframerate()!=22050 or source.getnframes()!=67601:
            raise RuntimeError('WELCOME_AUDIO_FORMAT_INVALID')
        with wave.open(filename,'wb') as target:
            target.setparams(source.getparams())
            while True:
                block=source.readframes(8192)
                if not block:break
                if amplitude!=100:
                    pcm=array('h',block)
                    if sys.byteorder!='little':pcm.byteswap()
                    for i in range(len(pcm)):pcm[i]=int(pcm[i]*amplitude/100.)
                    if sys.byteorder!='little':pcm.byteswap()
                    block=pcm.tobytes()
                target.writeframesraw(block)
    return {'engine':'sherpa-onnx-vits-piper-en_US-lessac','version':'welcome-en-lessac-20261025-01',
            'source':'VERIFIED_FIXED_PHRASE','sha256':WELCOME_SHA256,'synthesis_seconds':0,
            'timing':'PREGENERATED_SPEED_0.90','leading_silence_ms':500,'letter_break_ms':None}

def volume_preference():
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not VOLUME_FILE.exists():return {'boot_id':boot,'percent':100}
    data=json.loads(VOLUME_FILE.read_text())
    if data.get('boot_id')!=boot:return {'boot_id':boot,'percent':100}
    if data.get('schema')!='eeg.audio.volume.runtime.v1' or type(data.get('percent')) is not int or not 0<=data['percent']<=100:
        raise RuntimeError('VOLUME_RUNTIME_CONFIGURATION_INVALID')
    return data

def mixer(command,card,*parameters):
    p=subprocess.run([AMIXER,'-c',card,command,*parameters],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=.75)
    if p.returncode:raise RuntimeError('USB_PLAYBACK_MIXER_ERROR: '+p.stdout.decode('utf-8','replace')[-160:])
    return p.stdout.decode('utf-8','strict')

def volume_hardware():
    pcm,device=selected_usb_device(configuration().get('device'))
    text=mixer('cget',device['card_id'],'name=PCM Playback Volume')
    limits=re.search(r'min=(-?\d+),max=(-?\d+)',text);values=re.search(r': values=([0-9,\-]+)',text)
    switch=mixer('cget',device['card_id'],'name=PCM Playback Switch')
    enabled=re.search(r': values=(on|off)',switch)
    if not limits or not values or not enabled:raise RuntimeError('USB_PLAYBACK_VOLUME_CONTROL_UNSUPPORTED')
    low,high=map(int,limits.groups());raw=[int(v) for v in values.group(1).split(',')]
    if high<=low or not 1<=len(raw)<=2:raise RuntimeError('USB_PLAYBACK_VOLUME_RANGE_INVALID')
    return {'card_id':device['card_id'],'minimum':low,'maximum':high,'raw':raw,'switch_on':enabled.group(1)=='on','pcm_state':device['state'],
        'percent':round((sum(raw)/len(raw)-low)*100/(high-low)) if enabled.group(1)=='on' else 0,'device':pcm}

def volume_info():
    desired=volume_preference()['percent'];result={'volume_percent':desired,'volume_actual_percent':None,'volume_applied':False}
    try:
        actual=volume_hardware();result.update(volume_actual_percent=actual['percent'],volume_applied=abs(actual['percent']-desired)<=1,volume_control='PCM Playback Volume / Switch')
    except (OSError,RuntimeError,subprocess.SubprocessError) as error:result['volume_reason']=str(error)
    return result

def volume_apply(percent,initialize=False):
    if type(percent) is not int or not 0<=percent<=100:raise RuntimeError('VOLUME_MUST_BE_0_TO_100')
    VOLUME_FILE.parent.mkdir(mode=0o700,exist_ok=True)
    lock=os.open(str(VOLUME_FILE.parent/'volume.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        deadline=time.monotonic()+.5
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('VOLUME_CONTROL_BUSY_NO_REPLAY')
                time.sleep(.02)
        if initialize:percent=volume_preference()['percent']
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:actual=volume_hardware()
        except RuntimeError as error:
            if 'USB_AUDIO_DISCONNECTED_OR_AMBIGUOUS' not in str(error):raise
            actual=None
        applied=False
        if actual is not None:
            target=round(actual['minimum']+(actual['maximum']-actual['minimum'])*percent/100)
            if any(v!=target for v in actual['raw']):mixer('cset',actual['card_id'],'name=PCM Playback Volume',','.join([str(target)]*len(actual['raw'])))
            if actual['switch_on']!=(percent>0):mixer('cset',actual['card_id'],'name=PCM Playback Switch','on' if percent else 'off')
            checked=volume_hardware();applied=all(v==target for v in checked['raw']) and checked['switch_on']==(percent>0)
            if not applied:raise RuntimeError('VOLUME_WRITE_READBACK_NOT_CONFIRMED_NO_REPLAY')
        data={'schema':'eeg.audio.volume.runtime.v1','boot_id':boot,'percent':percent}
        pending=VOLUME_FILE.parent/('.volume-'+str(os.getpid())+'.json');pending.write_text(json.dumps(data)+'\n');pending.chmod(0o600);os.replace(str(pending),str(VOLUME_FILE))
        result={'schema':'eeg.audio.volume.result.v1','percent':percent,'applied_now':applied,'initialize':initialize,'persistence':'UNTIL_BOARD_REBOOT_NOT_UI_RESTART','reason':None if applied else 'USB音响未连接；本次开机偏好已保存，下次播放前应用',
            'during_playback':actual is not None and actual['pcm_state']=='BUSY','pid':os.getpid(),'parent_pid':os.getppid(),'ui_session':os.getenv('EEG_UI_SESSION'),'boot_id':boot,'monotonic_seconds':time.monotonic(),'targets':['PCM Playback Volume','PCM Playback Switch'],'mic_controls_modified':False}
        result_path=VOLUME_FILE.parent/'.volume-result.json';result_path.write_text(json.dumps(result)+'\n');os.replace(str(result_path),str(VOLUME_FILE.parent/'last_volume_result.json'))
        return result
    finally:os.close(lock)

def stage(value):
    print('EEG_SPEECH_STAGE '+json.dumps({'stage':value},sort_keys=True),flush=True)

def record_result(args,selected,info,synthesis):
    error=sys.exc_info()[1]
    status='CANCELLED' if isinstance(error,SpeechCancelled) else 'FAILED' if error else 'COMPLETED'
    row={'schema':'eeg.speech.result.v2','status':status,'language':args.lang,'utf8_bytes':len(args.text.encode('utf-8')),
         'speed_wpm':None if args.lang=='en' else args.speed,'requested_rate_setting':args.speed,'word_gap_10ms_at_default_speed':None if args.lang=='en' else args.word_gap,'device':selected,'wav':info,
         'amplitude':args.amplitude,'english_leading_silence_ms':synthesis.get('leading_silence_ms',350) if args.lang=='en' and synthesis else 0,
         'eeg_letter_break_ms':synthesis.get('letter_break_ms',150) if args.lang=='en' and synthesis else None,
         'eeg_acronym_spelled':args.lang=='en' and bool(re.search(r'\bEEG\b',args.text,re.I)),
         'synthesis':synthesis,'playback_requested':args.play,'hearing_confirmed':False,'pid':os.getpid(),
         'parent_pid':os.getppid(),'ui_session':os.getenv('EEG_UI_SESSION'),'monotonic_seconds':time.monotonic(),
         'error':str(error)[:512] if error else None}
    root=Path('/run/eeg-speech');encoded=(json.dumps(row,sort_keys=True)+'\n').encode('utf-8')
    pending=root/('.last-'+str(os.getpid())+'.json');pending.write_bytes(encoded);os.replace(str(pending),str(root/'last_result.json'))
    log=root/'speech.jsonl'
    if log.exists() and log.stat().st_size>256*1024:os.replace(str(log),str(root/'speech.jsonl.1'))
    with log.open('ab') as f:f.write(encoded)

class SpeechCancelled(RuntimeError):pass

def pad_english_onset(filename):
    """Bounded 350ms digital silence lets a USB amplifier wake before the first phoneme."""
    pending=filename+'.onset'
    try:
        with wave.open(filename,'rb') as source:
            if source.getnchannels()!=1 or source.getsampwidth()!=2 or source.getframerate()!=22050 or source.getnframes()*2>32*1024*1024-16000:
                raise RuntimeError('ENGLISH_WAV_ONSET_FORMAT_OR_SIZE_LIMIT')
            with wave.open(pending,'wb') as target:
                target.setparams(source.getparams());target.writeframesraw(b'\0'*(int(22050*.350)*2))
                while True:
                    block=source.readframes(8192)
                    if not block:break
                    target.writeframesraw(block)
        os.chmod(pending,0o600);os.replace(pending,filename)
    finally:
        if os.path.exists(pending):os.unlink(pending)

def cancel_signal(signum,frame):
    raise SpeechCancelled('CANCELLED_BY_OPERATOR')

def child_run(arguments,timeout=600):
    child=subprocess.Popen(arguments,stdin=subprocess.DEVNULL)
    try:
        rc=child.wait(timeout=timeout)
        if rc:raise subprocess.CalledProcessError(rc,arguments)
    finally:
        if child.poll() is None:
            child.terminate()
            try:child.wait(timeout=2)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=2)

def usb_playback_devices():
    result=[]
    for card in sorted(Path('/sys/class/sound').glob('card[0-9]*')):
        device=(card/'device').resolve()
        usb=next((p for p in [device]+list(device.parents) if (p/'idVendor').is_file() and (p/'idProduct').is_file()),None)
        if usb is None:continue
        number=int(card.name[4:]);root=Path('/proc/asound')/card.name
        for pcm in sorted(root.glob('pcm*p')):
            idx=pcm.name[3:-1]
            status=pcm/'sub0/status'
            state=status.read_text().strip() if status.exists() else 'UNKNOWN'
            result.append({'card':number,'card_id':(root/'id').read_text().strip(),'pcm':int(idx),
                'vendor':(usb/'idVendor').read_text().strip(),'product':(usb/'idProduct').read_text().strip(),
                'state':'READY' if state=='closed' else 'BUSY' if 'state:' in state else 'UNKNOWN'})
    return result

def selected_usb_device(selected):
    devices=usb_playback_devices()
    match=re.fullmatch(r'(?:plug)?hw:(?:CARD=)?([A-Za-z0-9_]+),(?:DEV=)?([0-9]+)',selected or '')
    if not match:raise RuntimeError('USB_AUDIO_PCM_SELECTION_REQUIRED')
    card,pcm=match.groups()
    matches=[d for d in devices if (str(d['card'])==card if card.isdigit() else d['card_id']==card) and d['pcm']==int(pcm)]
    if len(matches)!=1:raise RuntimeError('USB_AUDIO_DISCONNECTED_OR_AMBIGUOUS')
    d=matches[0]
    return 'plughw:CARD='+d['card_id']+',DEV='+str(d['pcm']),d

def probe():
    now=time.monotonic();result={'schema':'eeg.audio.probe.v1','monotonic_seconds':now,'audio_state':'UNKNOWN','reason':'','tts_ready':False}
    try:
        selected=configuration().get('device');pcm,dev=selected_usb_device(selected)
        result.update(audio_state=dev['state'],device=pcm,usb=dev)
    except (OSError,RuntimeError,ValueError) as error:
        result.update(audio_state='DISCONNECTED' if str(error)=='USB_AUDIO_DISCONNECTED_OR_AMBIGUOUS' else 'ERROR',reason=str(error))
    result['chinese_tts_ready']=Path('/run/eeg-tts/zh.sock').is_socket()
    result['english_tts_ready']=Path('/run/eeg-tts-en/en.sock').is_socket()
    # Existing Qt field explicitly describes Chinese readiness; English is separate.
    result['tts_ready']=result['chinese_tts_ready']
    result.update(volume_info())
    print(json.dumps(result,sort_keys=True),flush=True)
    return 0


def chinese_synthesize(text, speed, amplitude, filename, language='zh'):
    """One bounded request; disconnection/timeout is reported without replay."""
    config_path=CHINESE_CONFIG if language=='zh' else ENGLISH_CONFIG
    label='CHINESE' if language=='zh' else 'ENGLISH'
    expected_schema='eeg.chinese_tts.v1' if language=='zh' else 'eeg.english_tts.v1'
    expected_engine='sherpa-onnx-vits-piper-zh_CN-xiao_ya' if language=='zh' else 'sherpa-onnx-vits-piper-en_US-lessac'
    expected_socket='/run/eeg-tts/zh.sock' if language=='zh' else '/run/eeg-tts-en/en.sock'
    if not config_path.is_file():raise RuntimeError(label+'_TTS_NOT_CONFIGURED')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if (set(config) != {'schema', 'engine', 'version', 'socket'} or
            config['schema'] != expected_schema or
            config['engine'] != expected_engine or
            config['socket'] != expected_socket):
        raise RuntimeError(label+'_TTS_CONFIGURATION_INVALID')
    payload = (re.sub(r'\bEEG\b','E E G',text,flags=re.I) if language=='en' else text).encode('utf-8')
    start = time.monotonic()
    def receive(connection, length):
        data = bytearray()
        while len(data) < length:
            block = connection.recv(length - len(data))
            if not block:
                raise RuntimeError(label+'_TTS_CONNECTION_CLOSED_NO_REPLAY')
            data.extend(block)
        return bytes(data)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        try:
            connection.connect(config['socket'])
        except OSError as error:
            raise RuntimeError(label+'_TTS_NOT_READY: inspect matching eeg-'+('chinese' if language=='zh' else 'english')+'-tts service') from error
        connection.settimeout(600)
        connection.sendall(struct.pack('<If', len(payload), speed / 160.0 if language=='zh' else .90*speed/130.0) + payload)
        magic, rate, frames, size = struct.unpack('<4sIII', receive(connection, 16))
        if (magic != b'EEGW' or not 8000 <= rate <= 48000 or frames <= 0 or
                size != frames * 2 or size > 32 * 1024 * 1024 - 44):
            raise RuntimeError(label+'_TTS_RESPONSE_INVALID_NO_REPLAY')
        with wave.open(filename, 'wb') as output:
            output.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
            remaining = size
            while remaining:
                block = receive(connection, min(65536, remaining))
                pcm = array('h', block)
                if sys.byteorder != 'little':
                    pcm.byteswap()
                for i in range(len(pcm)):
                    pcm[i] = int(pcm[i] * amplitude / 100.0)
                if sys.byteorder != 'little':
                    pcm.byteswap()
                output.writeframesraw(pcm.tobytes())
                remaining -= len(block)
    return dict(engine=config['engine'], version=config['version'],
                synthesis_seconds=time.monotonic() - start)

def cached_chinese_synthesize(text,speed,amplitude,filename,language='zh'):
    """Exact-parameter successful WAV cache: 16 entries/8MiB/30min, RAM only, no text logs."""
    started=time.monotonic();config=json.loads((CHINESE_CONFIG if language=='zh' else ENGLISH_CONFIG).read_text(encoding='utf-8'))
    if set(config)!={'schema','engine','version','socket'} or config['schema']!=('eeg.chinese_tts.v1' if language=='zh' else 'eeg.english_tts.v1') or config['socket']!=('/run/eeg-tts/zh.sock' if language=='zh' else '/run/eeg-tts-en/en.sock'):
        raise RuntimeError('RESIDENT_TTS_CONFIGURATION_INVALID')
    root=Path('/run/eeg-speech/cache');root.mkdir(mode=0o700,exist_ok=True)
    # Reserve one <=2MiB result before synthesis, so cancellation cannot bypass bounds.
    prior=[]
    for meta in root.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{64}\.json',meta.name) or meta.is_symlink():continue
        wav=meta.with_suffix('.wav')
        if not wav.is_file() or wav.is_symlink():continue
        try:info=json.loads(meta.read_text());prior.append((info['last_used_monotonic'],wav.stat().st_size,meta,wav))
        except (OSError,ValueError,KeyError):continue
    prior.sort(key=lambda x:x[0]);prior_bytes=sum(x[1] for x in prior)
    while prior and (len(prior)>15 or prior_bytes>6*1024**2 or time.monotonic()-prior[0][0]>1800):
        _,size,meta,wav=prior.pop(0);wav.unlink();meta.unlink();prior_bytes-=size
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',config['version']):raise RuntimeError('CHINESE_TTS_VERSION_INVALID')
    model=Path('/opt/eeg/tts/releases')/config['version']/('model/zh_CN-xiao_ya-medium.onnx' if language=='zh' else 'model/en_US-lessac-medium.onnx');modelstat=model.stat()
    identity={'text':text,'speed':speed,'amplitude':amplitude,'config':config,'model':str(model.resolve()),'model_stat':[modelstat.st_size,modelstat.st_mtime_ns,modelstat.st_ino],
        'route_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    key=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    wavepath=root/(key+'.wav');metapath=root/(key+'.json');entry=None
    if wavepath.is_file() and metapath.is_file() and not wavepath.is_symlink() and not metapath.is_symlink() and Path(config['socket']).is_socket():
        try:
            entry=json.loads(metapath.read_text())
            if started-entry['last_used_monotonic']>1800 or wavepath.stat().st_size!=entry['bytes'] or entry['bytes']>2*1024**2 or hashlib.sha256(wavepath.read_bytes()).hexdigest()!=entry['sha256']:entry=None
        except (OSError,ValueError,KeyError):entry=None
    if entry is not None:
        shutil.copyfile(str(wavepath),filename);entry['last_used_monotonic']=time.monotonic();metapath.write_text(json.dumps(entry)+'\n')
        return {'engine':config['engine'],'version':config['version'],'source':'RAM_CACHE','cache_hit':True,'cache_fetch_seconds':time.monotonic()-started,'synthesis_seconds':None,'original_synthesis_seconds':entry['synthesis_seconds']}
    stage('SYNTHESIZING');result=chinese_synthesize(text,speed,amplitude,filename,language)
    with wave.open(filename,'rb') as checked:
        if checked.getnchannels()!=1 or checked.getsampwidth()!=2 or checked.getframerate()!=22050 or checked.getnframes()<=0:
            raise RuntimeError('SYNTHESIZED_WAV_FORMAT_INVALID_NOT_CACHED')
    result.update(source='LIVE_MODEL',cache_hit=False)
    size=Path(filename).stat().st_size
    if size<=2*1024**2:
        pending=root/('.'+key+'-'+str(os.getpid())+'.pending');meta_pending=root/('.'+key+'-'+str(os.getpid())+'.meta');published=False;wave_written=False
        try:
            shutil.copyfile(filename,str(pending));pending.chmod(0o600)
            entry={'bytes':size,'sha256':hashlib.sha256(pending.read_bytes()).hexdigest(),'last_used_monotonic':time.monotonic(),'synthesis_seconds':result['synthesis_seconds']}
            meta_pending.write_text(json.dumps(entry)+'\n');meta_pending.chmod(0o600)
            os.replace(str(pending),str(wavepath));wave_written=True;os.replace(str(meta_pending),str(metapath));published=True
        finally:
            for path in (pending,meta_pending):
                if path.exists():path.unlink()
            if wave_written and not published:
                for path in (wavepath,metapath):
                    if path.exists():path.unlink()
    entries=[]
    for meta in root.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{64}\.json',meta.name) or meta.is_symlink():continue
        wav=meta.with_suffix('.wav')
        if not wav.is_file() or wav.is_symlink():continue
        try:info=json.loads(meta.read_text());entries.append((info['last_used_monotonic'],wav.stat().st_size,meta,wav))
        except (OSError,ValueError,KeyError):continue
    entries.sort(key=lambda x:x[0]);total=sum(x[1] for x in entries);evicted=0
    while entries and (len(entries)>16 or total>8*1024**2 or time.monotonic()-entries[0][0]>1800):
        _,size,meta,wav=entries.pop(0);wav.unlink();meta.unlink();total-=size;evicted+=1
    result['cache_evicted_entries']=evicted;result['cache_max_bytes']=8*1024**2;result['cache_max_entries']=16
    return result


def device_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_=,:.\-]{1,160}', value):
        raise argparse.ArgumentTypeError('Use one ALSA PCM name from eeg-audio list.')
    return value


def configuration():
    if not CONFIG.exists():
        return {}
    data = json.loads(CONFIG.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'schema', 'device'} or data['schema'] != 'eeg.audio.v1':
        raise RuntimeError('AUDIO_CONFIGURATION_INVALID')
    try:
        device_name(data['device'])
    except (TypeError, argparse.ArgumentTypeError):
        raise RuntimeError('AUDIO_CONFIGURATION_INVALID')
    return data


def save_configuration(device):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.audio-', dir=str(CONFIG.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(dict(schema='eeg.audio.v1', device=device), stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, str(CONFIG))
        directory = os.open(str(CONFIG.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def audio_main(arguments):
    parser = argparse.ArgumentParser(description='Enumerate ALSA devices or save the explicitly selected playback PCM.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='Read ALSA playback devices and PCM names; emits no sound.')
    commands.add_parser('status', help='Read the persisted playback device selection.')
    selected = commands.add_parser('select', help='Persist an ALSA PCM name; emits no sound.')
    selected.add_argument('device', type=device_name)
    args = parser.parse_args(arguments)
    if args.command == 'list':
        for option in ('-l', '-L'):
            result = subprocess.run([APLAY, option], check=False)
            if result.returncode:
                return result.returncode
    elif args.command == 'select':
        save_configuration(args.device)
        print('EEG_AUDIO_SELECTED ' + args.device)
    else:
        data = configuration()
        print('EEG_AUDIO_SELECTED ' + data['device'] if data else 'EEG_AUDIO_UNCONFIGURED')
    return 0


def speech_main(arguments):
    parser = argparse.ArgumentParser(description='Synthesize Chinese/English speech; playback requires --play.')
    parser.add_argument('--lang', choices=('zh', 'en'), default='zh')
    parser.add_argument('--device', type=device_name, help='Override persisted ALSA PCM for this invocation.')
    parser.add_argument('--play', action='store_true', help='Explicitly play the generated finite WAV.')
    parser.add_argument('--wav', type=Path, help='Save a WAV without overwriting an existing file.')
    parser.add_argument('--speed', type=int, default=None,help='Default: Chinese160, English130 words/min.')
    parser.add_argument('--word-gap',type=int,default=None,help='English word gap in espeak 10ms-at-default-speed units; default4.')
    parser.add_argument('--amplitude', type=int, default=None)
    parser.add_argument('--text-stdin',action='store_true',help='Read one bounded UTF-8 payload from stdin; bypass locale-dependent argv encoding.')
    parser.add_argument('text',nargs='?',help='UTF-8 text, at most 4096 characters; quote it as one argument.')
    args = parser.parse_args(arguments)
    if args.speed is None:args.speed=130 if args.lang=='en' else 160
    if args.word_gap is None:args.word_gap=4 if args.lang=='en' else 0
    if args.amplitude is None:args.amplitude=100 if args.lang=='en' else 80
    if args.text_stdin:
        if args.text is not None:parser.error('Choose exactly stdin or positional text.')
        payload=sys.stdin.buffer.read(16385)
        if len(payload)>16384:parser.error('UTF8_STDIN_BYTE_LIMIT')
        args.text=payload.decode('utf-8','strict')
    if args.text is None:parser.error('Text required.')
    if not args.play and args.wav is None:
        parser.error('Choose --wav FILE or explicitly request --play.')
    if not args.text.strip() or len(args.text) > 4096 or '\0' in args.text:
        parser.error('Text must contain 1 to 4096 characters without NUL.')
    if not 80 <= args.speed <= 450 or not 0 <= args.amplitude <= 100 or not 0<=args.word_gap<=20:
        parser.error('Speed must be 80..450 and amplitude must be 0..100.')
    selected = args.device or configuration().get('device') if args.play else None
    if args.play and not selected:
        parser.error('Choose a playback PCM with eeg-audio select or --device first.')
    if args.play:
        selected,device=selected_usb_device(selected)
        if device['state']=='BUSY':raise RuntimeError('USB_AUDIO_BUSY')
        volume_apply(volume_preference()['percent'],initialize=True)
        if volume_preference()['percent']==0:raise RuntimeError('AUDIO_MUTED_VOLUME_ZERO')
    destination = args.wav.expanduser().absolute() if args.wav is not None else None
    if destination is not None:
        if destination.exists() or destination.is_symlink():
            raise RuntimeError('WAV_DESTINATION_EXISTS')
        if not destination.parent.is_dir():
            raise RuntimeError('WAV_PARENT_DIRECTORY_REQUIRED')
    runtime=Path('/run/eeg-speech');runtime.mkdir(mode=0o700,exist_ok=True)
    temporary_parent = str(destination.parent) if destination is not None else str(runtime)
    fd, temporary_name = tempfile.mkstemp(prefix='.eeg-speech-', suffix='.wav', dir=temporary_parent)
    os.close(fd)
    lock = None
    info=None;synthesis=None
    try:
        lock = os.open('/run/eeg-speech.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('EEG_SPEECH_BUSY') from error
        if args.lang == 'zh':
            synthesis = cached_chinese_synthesize(args.text, args.speed, args.amplitude, temporary_name)
        else:
            stage('SYNTHESIZING')
            synthesis=english_welcome(args.text,args.amplitude,temporary_name)
            if synthesis is None:
                synthesis=cached_chinese_synthesize(args.text,args.speed,args.amplitude,temporary_name,'en')
                pad_english_onset(temporary_name)
                synthesis.update(leading_silence_ms=350,letter_break_ms=None,timing='MODEL_SPEED_0.90_AT_DEFAULT')
        with wave.open(temporary_name, 'rb') as stream:
            info = dict(channels=stream.getnchannels(), sample_width=stream.getsampwidth(),
                        rate=stream.getframerate(), frames=stream.getnframes())
        if info['channels'] != 1 or info['sample_width'] != 2 or info['rate'] != 22050 or info['frames'] <= 0:
            raise RuntimeError('SYNTHESIZED_WAV_FORMAT_INVALID')
        if Path(temporary_name).stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError('SYNTHESIZED_WAV_SIZE_LIMIT')
        if destination is not None:
            sync_fd = os.open(temporary_name, os.O_RDWR)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)
            os.link(temporary_name, str(destination))
            print('EEG_SPEECH_WAV ' + str(destination))
        if args.play:
            volume_apply(volume_preference()['percent'],initialize=True)
            if volume_preference()['percent']==0:raise RuntimeError('AUDIO_MUTED_VOLUME_ZERO')
            stage('PLAYING')
            child_run([APLAY, '-q', '-D', selected, temporary_name],timeout=600)
            print('EEG_SPEECH_PLAYED ' + selected)
        print(json.dumps(dict(schema='eeg.speech.v1', language=args.lang, wav=info,
                              playback_requested=args.play, synthesis=synthesis), sort_keys=True))
    finally:
        try:record_result(args,selected,info,synthesis)
        except OSError:pass
        if lock is not None:
            os.close(lock)
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    stage('COMPLETED')
    return 0


def main():
    signal.signal(signal.SIGTERM,cancel_signal)
    signal.signal(signal.SIGINT,cancel_signal)
    if sys.argv[1:]==['--probe']:return probe()
    if sys.argv[1:]==['--volume-get']:
        print(json.dumps(volume_info()));return 0
    if sys.argv[1:]==['--volume-init']:
        print(json.dumps(volume_apply(volume_preference()['percent'],initialize=True)));return 0
    if len(sys.argv)==3 and sys.argv[1]=='--volume-set':
        print(json.dumps(volume_apply(int(sys.argv[2]))));return 0
    if Path(sys.argv[0]).name == 'eeg-audio':
        return audio_main(sys.argv[1:])
    return speech_main(sys.argv[1:])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SpeechCancelled:
        stage('CANCELLED');raise SystemExit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('EEG_SPEECH_ERROR ' + str(error), file=sys.stderr)
        raise SystemExit(1)
