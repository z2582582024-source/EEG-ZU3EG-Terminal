#!/usr/bin/env python3
"""Independent, default-read-only, whole eMMC user-area backup restoration.

No SD installer entry, boot0/boot1, RPMB, EXT_CSD, QSPI, format or mount writes.
An interrupted attempt is never resumed/replayed automatically. Explicit recovery
can reference observed older UNKNOWN heads without modifying those old records.
"""
import argparse,contextlib,hashlib,importlib.util,ipaddress,json,os,re,stat,sys,urllib.parse,urllib.request,uuid
from pathlib import Path

_spec=importlib.util.spec_from_file_location('eeg_restore_ram_library',Path(__file__).with_name('eeg_emmc_ram_install.py'))
ram=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(ram)
legacy=ram.legacy
REVISION='ram-restore-20261023-01'
MAX_CHUNK=8*1024**2
SAMPLE_BYTES=65536
LOCK=Path('/run/eeg-operator/maintenance.lock')


def fail(message):raise RuntimeError(message)


def read_exact(stream,length):
    """Accept legal short reads while bounding each current chunk in memory."""
    data=bytearray()
    while len(data)<length:
        part=stream.read(length-len(data))
        if not part:break
        data.extend(part)
    return bytes(data)


def ident(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):fail('EXACT_BACKUP_UUIDHEX_REQUIRED')
    return value


def environment_proof(path,tool_sha,mounts):
    actual=legacy.sha(Path(__file__))
    if tool_sha!=actual or not ram.valid_sha(tool_sha):fail('PINNED_RESTORE_TOOL_SHA_REQUIRED')
    ram.no_symlink_components(path)
    environment=legacy.json_read(path)
    if environment.get('schema')!='eeg.zu3eg.jtag_environment.v1' or environment.get('product')!='EEG-ZU3EG' or environment.get('revision')!=ram.REVISION:
        fail('MATCHED_CURRENT_JTAG_RAM_ENVIRONMENT_REQUIRED')
    roots=[row for row in mounts if row['mountpoint']=='/']
    if len(roots)!=1 or roots[0]['fstype'] not in ram.RAM_FS or legacy.find_mount(path,mounts)['fstype'] not in ram.RAM_FS:
        fail('RESTORE_MUST_RUN_FROM_INDEPENDENT_RAM_ROOT')
    if 'eeg_jtag_install=1' not in Path('/proc/cmdline').read_text().split():fail('CURRENT_RAM_BOOT_MARKER_REQUIRED')
    if os.uname().release!=environment.get('kernel_release'):fail('RAM_KERNEL_ENVIRONMENT_MISMATCH')
    legacy.identity_equal(legacy.json_read(Path('/etc/eeg-system/baseline.json')),environment.get('system_identity',{}))
    for key,value in (('installer_sha256',legacy.sha(Path(ram.__file__))),('validator_sha256',legacy.sha(Path(legacy.__file__)))):
        if environment.get(key)!=value:fail('RAM_LIBRARY_BINDING_MISMATCH:'+key)
    if not ram.valid_sha(environment.get('platform_xsa_sha256')):fail('PINNED_PLATFORM_XSA_REQUIRED')
    if environment.get('restore_sha256',actual)!=actual:fail('RAM_RESTORE_BINDING_MISMATCH')
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch('[0-9a-f-]{36}',boot_id):fail('CURRENT_RAM_BOOT_ID_REQUIRED')
    return {'sha256':legacy.sha(path),'boot_id':boot_id,'root':roots[0],'kernel_release':environment['kernel_release'],
            'restore_tool_sha256':actual,'library_sha256':environment['installer_sha256'],'validator_sha256':environment['validator_sha256']}


def off_target_host(base):
    parts=urllib.parse.urlsplit(ram.url_base(base))
    if parts.path not in ('','/'):fail('BACKUP_HOST_ROOT_URL_REQUIRED')
    try:address=ipaddress.IPv4Address(parts.hostname)
    except ValueError:fail('EXPLICIT_PRIVATE_IPV4_BACKUP_HOST_REQUIRED')
    if not address.is_private or address.is_loopback or address.is_unspecified:fail('OFF_TARGET_PRIVATE_BACKUP_HOST_REQUIRED')
    text=Path('/proc/net/fib_trie').read_text()
    local=set(re.findall(r'\|--\s+(\d+\.\d+\.\d+\.\d+)\s*\n\s*/32 host LOCAL',text))
    if not local or str(address) in local:fail('BACKUP_SOURCE_MUST_BE_VERIFIABLY_OFF_TARGET')
    return {'base_url':base.rstrip('/'),'source_is_off_target':True}


def backup_source(base,backup_id,cid,expected_sha=None,requester=ram.request_json):
    ident(backup_id);ram.require_cid(cid)
    value=requester(base,'/v1/backup/'+backup_id+'/status')
    meta,complete=value.get('metadata'),value.get('complete')
    if not isinstance(meta,dict) or not isinstance(complete,dict) or value.get('verified') is not True or value.get('current_full_backup_readback_verified') is not True:
        fail('CURRENT_COMPLETE_HOST_BACKUP_READBACK_REQUIRED')
    if meta.get('schema')!='eeg.zu3eg.emmc_backup.v1' or meta.get('area')!='user_area':fail('RESTORE_USER_AREA_ONLY_BOOT_RPMB_QSPI_FORBIDDEN')
    if meta.get('target_cid')!=cid or complete.get('target_cid')!=cid or complete.get('area')!='user_area':fail('BACKUP_TARGET_CID_OR_AREA_MISMATCH')
    size,chunk=meta.get('size_bytes'),meta.get('chunk_bytes')
    if type(size) is not int or not 0<size<=64*1024**3 or type(chunk) is not int or not 0<chunk<=MAX_CHUNK:fail('BOUNDED_BACKUP_GEOMETRY_REQUIRED')
    whole=complete.get('sha256')
    if not ram.valid_sha(whole) or expected_sha is not None and whole!=expected_sha:fail('PINNED_COMPLETE_BACKUP_SHA_REQUIRED')
    if complete.get('verified') is not True or complete.get('bytes')!=size or complete.get('size_bytes')!=size or complete.get('metadata_sha256')!=ram.digest(ram.canonical(meta)):
        fail('COMPLETE_BACKUP_METADATA_BINDING_REQUIRED')
    if not isinstance(complete.get('backup_location'),str) or not complete['backup_location']:fail('EXACT_HOST_BACKUP_LOCATION_REQUIRED')
    chunks=value.get('chunks');count=(size+chunk-1)//chunk
    if not isinstance(chunks,list) or len(chunks)!=count or complete.get('chunk_count')!=count:fail('EXACT_COMPLETE_BACKUP_CHUNK_INVENTORY_REQUIRED')
    for sequence,row in enumerate(chunks):
        if not isinstance(row,dict) or row.get('sequence')!=sequence or row.get('bytes')!=min(chunk,size-sequence*chunk) or not ram.valid_sha(row.get('sha256')):
            fail('BACKUP_CHUNK_SEQUENCE_LENGTH_OR_SHA_INVALID')
    return {'backup_id':backup_id,'target_cid':cid,'area':'user_area','bytes':size,'chunk_bytes':chunk,'sha256':whole,
            'metadata_sha256':complete['metadata_sha256'],'host_backup_location':complete['backup_location'],'chunks':chunks}


def process_observation(target,exclude_self=False):
    devnos={target['major_minor']}|{row['major_minor'] for row in target['partitions']}
    owners=[];writers=[];incomplete=False
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or exclude_self and int(process.name)==os.getpid():continue
        try:
            args=(process/'cmdline').read_bytes().split(b'\0')
            names={Path(os.fsdecode(arg)).name for arg in args if arg}
            tools={'eeg_emmc_ram_install.py','eeg_emmc_ram_restore.py','eeg_emmc_install.py','burn-emmc.sh'}
            if names&tools and b'apply' in args:writers.append({'pid':int(process.name),'tools':sorted(names&tools)})
            for fd in (process/'fd').iterdir():
                try:metadata=fd.stat()
                except FileNotFoundError:continue
                if stat.S_ISBLK(metadata.st_mode) and str(os.major(metadata.st_rdev))+':'+str(os.minor(metadata.st_rdev)) in devnos:
                    owners.append({'pid':int(process.name),'fd':fd.name})
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError:incomplete=True
    return {'openers':sorted(owners,key=lambda row:(row['pid'],row['fd'])),
            'active_storage_writers':sorted(writers,key=lambda row:row['pid']),'complete':not incomplete}


@contextlib.contextmanager
def open_device(target,writable=False):
    import fcntl
    path=Path(target['device'])
    if not re.fullmatch('/dev/mmcblk[0-9]+',str(path)) or path.is_symlink():fail('ONLY_WHOLE_EMMC_USER_DEVICE_ALLOWED')
    flags=(os.O_RDWR if writable else os.O_RDONLY)|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC
    fd=os.open(str(path),flags)
    try:
        metadata=os.fstat(fd)
        if not stat.S_ISBLK(metadata.st_mode) or str(os.major(metadata.st_rdev))+':'+str(os.minor(metadata.st_rdev))!=target['major_minor']:
            fail('EXCLUSIVE_TARGET_DESCRIPTOR_IDENTITY_MISMATCH')
        size=int.from_bytes(fcntl.ioctl(fd,0x80081272,b'\0'*8),'little')
        if size!=target['bytes']:fail('EXCLUSIVE_TARGET_DESCRIPTOR_LENGTH_MISMATCH')
        with os.fdopen(fd,'r+b' if writable else 'rb',buffering=0) as stream:
            fd=None;yield stream
    finally:
        if fd is not None:os.close(fd)


def target_sample(target,device_opener=open_device):
    with device_opener(target,False) as stream:
        count=min(SAMPLE_BYTES,target['bytes']);first=read_exact(stream,count);stream.seek(target['bytes']-count);last=read_exact(stream,count)
    if len(first)!=count or len(last)!=count:fail('READ_ONLY_TARGET_SAMPLE_SHORT_READ')
    return {'first_offset':0,'last_offset':target['bytes']-count,'bytes_each':count,'sha256':ram.digest(first+last),'not_whole_target_verification':True}


def current_target(target,include_idle=True):
    rows=[row for row in legacy.mmc_disks() if row['cid']==target['cid'] and row['type']=='MMC' and not row['removable']]
    keys=('device','name','cid','bytes','major_minor','read_only')
    if len(rows)!=1 or any(rows[0][key]!=target[key] for key in keys):fail('RESTORE_TARGET_IDENTITY_CHANGED')
    if ram.target_blockers(rows[0],legacy.read_mounts()):fail('RESTORE_TARGET_MOUNT_SWAP_HOLDERS_OR_READ_ONLY')
    observed=process_observation(rows[0],exclude_self=True)
    if not observed['complete'] or observed['openers'] or observed['active_storage_writers']:fail('RESTORE_TARGET_OPEN_OR_PROCESS_OBSERVATION_INCOMPLETE')
    if include_idle:legacy.idle_proof(maintenance_lock_held=True)


def build_plan(base,backup_id,cid,environment,tool_sha,backup_sha=None,lock_held=False):
    mounts=legacy.read_mounts();env=environment_proof(environment,tool_sha,mounts);source_host=off_target_host(base)
    source=backup_source(base,backup_id,cid,backup_sha)
    candidates=[row for row in legacy.mmc_disks() if row['type']=='MMC' and not row['removable'] and row['cid']==cid]
    blockers=[];target=candidates[0] if len(candidates)==1 else None
    if target is None:blockers.append('EXACTLY_ONE_BACKUP_MATCHED_EMMC_REQUIRED')
    elif target['bytes']!=source['bytes']:blockers.append('EXACT_SAME_FULL_USER_AREA_CAPACITY_REQUIRED')
    else:blockers.extend(ram.target_blockers(target,mounts))
    observed=process_observation(target,exclude_self=lock_held) if target else None
    if observed and (not observed['complete'] or observed['openers'] or observed['active_storage_writers']):blockers.append('TARGET_OPEN_OR_LIVE_WRITER_OR_PROCESS_VISIBILITY_INCOMPLETE')
    try:idle=legacy.idle_proof(maintenance_lock_held=lock_held)
    except (OSError,RuntimeError,ValueError) as error:idle=None;blockers.append(str(error))
    if ram.available_memory()<ram.RAM_RESERVE+2*source['chunk_bytes']:blockers.append('BOUNDED_RESTORE_AND_KERNEL_RAM_RESERVE_REQUIRED')
    history=ram.request_json(base,'/v1/journal/target/'+cid).get('attempts')
    if not isinstance(history,list):fail('CURRENT_TARGET_HOST_JOURNAL_REQUIRED')
    prior=[row for row in history if row.get('terminal') is not True]
    marker=LOCK.with_name('maintenance.active')
    if marker.is_symlink():fail('MAINTENANCE_MARKER_SYMLINK_REJECTED')
    sample=target_sample(target) if target and not blockers else None
    plan={'schema':'eeg.zu3eg.emmc_restore_plan.v1','revision':REVISION,'environment':env,'source_host':source_host,'source':source,'target':target,
        'live_maintenance_state':idle,'process_observation':observed,'target_content_sample':sample,'prior_host_attempts':history,
        'prior_unknown_heads':prior,'prior_maintenance_marker_sha256':legacy.sha(marker) if marker.exists() else None,
        'requires_explicit_unknown_recovery_observations':bool(prior or marker.exists()),
        'overwrite':None if target is None else {'area':'user_area','offset':0,'bytes':target['bytes']},
        'excluded_from_writes':['boot0','boot1','RPMB','EXT_CSD','QSPI'],'blockers':blockers,'software_plan_clear':not blockers,'can_apply':False}
    plan['plan_sha256']=ram.digest(ram.canonical(plan));return plan


def validate_consent(record,plan,attempt):
    ident(attempt)
    target,source=plan['target'],plan['source']
    expected={'schema':'eeg.zu3eg.emmc_restore_consent.v1','decision':'RESTORE_WHOLE_USER_AREA_FROM_VERIFIED_BACKUP','attempt':attempt,
        'target_cid':target['cid'],'target_bytes':target['bytes'],'backup_id':source['backup_id'],'backup_sha256':source['sha256'],
        'plan_sha256':plan['plan_sha256'],'ram_boot_id':plan['environment']['boot_id'],'environment_sha256':plan['environment']['sha256']}
    if not isinstance(record,dict) or any(record.get(key)!=value for key,value in expected.items()) or record.get('operator_approved_restore') is not True or not isinstance(record.get('operator_confirmation_text'),str) or len(record['operator_confirmation_text'])<8:
        fail('CURRENT_SPECIFIC_RESTORE_CONFIRMATION_REQUIRED')
    if any(row.get('attempt')==attempt for row in plan['prior_host_attempts']):fail('RESTORE_ATTEMPT_ALREADY_USED_NEVER_REPLAY')
    observations=record.get('recovery_observations',{})
    if plan['requires_explicit_unknown_recovery_observations']:
        if (not isinstance(observations,dict) or observations.get('prior_unknown_heads')!=plan['prior_unknown_heads'] or
            observations.get('target_sample_sha256')!=plan['target_content_sample']['sha256'] or
            observations.get('process_observation_sha256')!=ram.digest(ram.canonical(plan['process_observation'])) or
            observations.get('preserved_maintenance_marker_sha256')!=plan['prior_maintenance_marker_sha256'] or
            observations.get('decision')!='OBSERVED_NO_LIVE_WRITER_RESTORE_WITHOUT_CLEARING_OLD_UNKNOWN'):
            fail('EXPLICIT_CURRENT_UNKNOWN_PID_JOURNAL_AND_CONTENT_OBSERVATIONS_REQUIRED')
    return record


def restore_worker(plan,journal,receipt,opener=urllib.request.urlopen,device_opener=open_device,guard=current_target):
    target,source=plan['target'],plan['source'];guard(target,True)
    if source['area']!='user_area' or source['bytes']!=target['bytes'] or source['target_cid']!=target['cid']:fail('RESTORE_SOURCE_TARGET_SCOPE_MISMATCH')
    with device_opener(target,True) as stream:
        stream.seek(0)
        for row in source['chunks']:
            sequence,length=row['sequence'],row['bytes'];offset=sequence*source['chunk_bytes']
            request=urllib.request.Request(plan['source_host']['base_url']+'/v1/backup/'+source['backup_id']+'/chunks/'+format(sequence,'06d'),headers={'Accept-Encoding':'identity'})
            with opener(request,timeout=120) as response:
                if response.status!=200:fail('WHOLE_BACKUP_CHUNK_RESPONSE_REQUIRED')
                data=response.read(length+1)
            if len(data)!=length or ram.digest(data)!=row['sha256']:fail('RESTORE_SOURCE_CHUNK_LENGTH_OR_SHA_MISMATCH')
            guard(target,sequence%16==0)
            journal.event('BEFORE_RESTORE_CHUNK','write_verified_user_area_chunk',{'offset':offset,'bytes':length,'sha256':row['sha256']})
            receipt['target_write_may_have_started']=True
            stream.seek(offset);view=memoryview(data);written=0
            while written<length:
                count=stream.write(view[written:])
                if not count:fail('RESTORE_PARTIAL_WRITE_NO_AUTOMATIC_RETRY')
                written+=count;receipt['bytes_written']=receipt.get('bytes_written',0)+count
            del view,data
            receipt['chunks_written']=sequence+1
            if (sequence+1)%16==0 or sequence==len(source['chunks'])-1:
                stream.flush();os.fsync(stream.fileno())
                journal.event('AFTER_RESTORE_SYNC','durable_user_area_prefix',{'through_offset':offset+length})
        stream.flush();os.fsync(stream.fileno());journal.event('BEFORE_RESTORE_READBACK','full_user_area_readback',{'bytes':source['bytes'],'sha256':source['sha256']})
        stream.seek(0);whole=hashlib.sha256();read_bytes=0
        for row in source['chunks']:
            data=read_exact(stream,row['bytes'])
            if len(data)!=row['bytes'] or ram.digest(data)!=row['sha256']:fail('RESTORE_FULL_TARGET_READBACK_CHUNK_MISMATCH')
            whole.update(data);read_bytes+=len(data)
        if read_bytes!=source['bytes'] or stream.read(1) or whole.hexdigest()!=source['sha256']:fail('RESTORE_FULL_TARGET_READBACK_SHA_OR_LENGTH_MISMATCH')
        receipt.update(bytes_readback=read_bytes,target_readback_sha256=whole.hexdigest(),full_user_area_readback_verified=True)


def apply(args):
    import fcntl
    if os.geteuid()!=0:fail('ROOT_REQUIRED_FOR_EMMC_USER_AREA_RESTORE')
    cid=ram.require_cid(args.target_cid);ident(args.attempt)
    if args.confirm_restore!=cid or args.consent is None or not ram.valid_sha(args.backup_sha256):fail('EXACT_CID_BACKUP_SHA_AND_CURRENT_RESTORE_CONSENT_REQUIRED')
    journal=ram.HostJournal(args.host_url,args.attempt,cid)
    ram.no_symlink_components(LOCK)
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    marker=LOCK.with_name('maintenance.active');owned_marker=False
    receipt={'schema':'eeg.zu3eg.emmc_ram_restore_receipt.v1','attempt':args.attempt,'result':'INCOMPLETE','target_write_may_have_started':False,
             'bytes_written':0,'emmc_boot':'NOT_EXERCISED','reboot_performed':False,'excluded_from_writes':['boot0','boot1','RPMB','EXT_CSD','QSPI']}
    with LOCK.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            plan=build_plan(args.host_url,args.backup_id,cid,args.environment,args.tool_sha256,args.backup_sha256,True)
            if plan['blockers']:fail('RESTORE_BLOCKED:'+','.join(plan['blockers']))
            record=validate_consent(legacy.json_read(args.consent),plan,args.attempt)
            receipt['plan']=plan;receipt['consent_sha256']=legacy.sha(args.consent)
            if not marker.exists():
                with marker.open('x') as stream:stream.write('emmc-ram-restore '+args.attempt+'\n')
                owned_marker=True
            journal.event('AUTHORIZED','confirmed_user_area_restore',{'plan_sha256':plan['plan_sha256'],'consent_sha256':receipt['consent_sha256'],
                'pid':os.getpid(),'boot_id':plan['environment']['boot_id'],'preserved_prior_unknown_heads':plan['prior_unknown_heads'],
                'explicit_recovery_observations':record.get('recovery_observations')})
            restore_worker(plan,journal,receipt)
            receipt['result']=ram.TERMINAL_PHASE
            journal.event(ram.TERMINAL_PHASE,'restore_complete_boot_pending',receipt)
            if owned_marker:
                if marker.is_symlink() or marker.read_text()!='emmc-ram-restore '+args.attempt+'\n':fail('OWN_RESTORE_MARKER_CHANGED_OBSERVE_WITHOUT_REMOVING')
                marker.unlink()
        except Exception as error:
            receipt['error']=str(error);receipt['result']='FAILED_UNKNOWN' if receipt['target_write_may_have_started'] else 'REJECTED_BEFORE_WRITE'
            with contextlib.suppress(Exception):journal.event(receipt['result'],'observe_before_any_new_restore',receipt)
            if owned_marker and not receipt['target_write_may_have_started']:
                if not marker.is_symlink() and marker.read_text()=='emmc-ram-restore '+args.attempt+'\n':marker.unlink()
            raise
        finally:print(json.dumps(receipt,ensure_ascii=False),flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','apply'),nargs='?',default='plan')
    parser.add_argument('--host-url',required=True);parser.add_argument('--backup-id',required=True);parser.add_argument('--target-cid',required=True)
    parser.add_argument('--backup-sha256');parser.add_argument('--environment',type=Path,default=ram.ENVIRONMENT);parser.add_argument('--tool-sha256',required=True)
    parser.add_argument('--attempt',default=uuid.uuid4().hex);parser.add_argument('--confirm-restore');parser.add_argument('--consent',type=Path)
    args=parser.parse_args(argv)
    if args.action=='plan':
        plan=build_plan(args.host_url,args.backup_id,ram.require_cid(args.target_cid),args.environment,args.tool_sha256,args.backup_sha256)
        print(json.dumps(plan,ensure_ascii=False,indent=2));return 0 if plan['software_plan_clear'] else 3
    apply(args);return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print('EEG_EMMC_USER_AREA_RESTORE_FAILED: '+str(error),file=sys.stderr);raise SystemExit(3)
