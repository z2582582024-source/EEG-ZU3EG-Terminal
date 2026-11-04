#!/usr/bin/env python3
"""Narrow, default-plan recovery of the observed empty ext4 root partition.

No sfdisk, mkfs, partition-table write or boot-file copy exists in this entry.
Old FAILED_UNKNOWN journal and maintenance.active are evidence, never cleared.
"""
import argparse,contextlib,gzip,hashlib,importlib.util,io,json,os,re,shutil,stat,struct,subprocess,sys,tarfile,tempfile,uuid
from pathlib import Path

for _name,_sha in (('eeg_emmc_ram_install.py','12c1490fc7a38ed8f28ecc1a6c3653aa0296133cfc10787ca234ecc8d416c9df'),
                   ('eeg_emmc_install.py','3995c834ad03371fec9ce0e4f11d7fd32eb207ecd8d1e06d4d4301847069e5ec'),
                   ('eeg_emmc_ram_restore.py','beab5cb7ce892fa0300c648982ec2abdd0e0069bcae8ee8b1e84d8bd0b136a9c')):
    _path=Path(__file__).with_name(_name)
    if _path.is_symlink() or hashlib.sha256(_path.read_bytes()).hexdigest()!=_sha:
        raise RuntimeError('PINNED_UNCHANGED_RECOVERY_DEPENDENCY_REQUIRED:'+_name)
_spec=importlib.util.spec_from_file_location('eeg_rootfs_recovery_restore',Path(__file__).with_name('eeg_emmc_ram_restore.py'))
restore=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(restore)
ram,legacy=restore.ram,restore.legacy
REVISION='rootfs-only-recovery-20261023-01'
PRIOR_ATTEMPT='c67d1f3e922945c8bee33feb973fe5bc'
PRIOR_SEQUENCE=21
PRIOR_HEAD_SHA='3d6ac898abaeb3b6ad4e3f65cf9e5203c55392cc0f7ee311189665d7a84b13fb'
INITIAL_OBSERVATION_SHA='7466d6c316544b4ddc5d55cbb47465fc1946200dc9976d27c79bd773c07f2d4e'
EXPECTED={'cid':'15010038475546345239c20452dc7b00','bytes':7818182656,'disk_id':'d3edca75',
          'boot_start':2048,'boot_sectors':1048576,'root_start':1050624,'root_sectors':14219264,
          'root_uuid':'2c1748fc-e030-42cc-b367-14fb35e190dc','boot_uuid':'6ADB-4D45',
          'ram_boot_id':'a3450ab4-faf2-4726-924e-022b2d502454'}
LOCK=Path('/run/eeg-operator/maintenance.lock')


def fail(message):raise RuntimeError(message)


def checked_command(argv,commands,journal=None):
    if argv[0] not in {'tar','mount','umount','blkid','sync','e2fsck','fsck.fat'}:
        fail('RECOVERY_COMMAND_OUTSIDE_NARROW_ALLOWLIST')
    return ram.run_command(argv,commands,journal) if journal else legacy.invoke(argv,commands)


def prior_journal(base,cid,requester=ram.request_json,opener=__import__('urllib.request',fromlist=['urlopen']).urlopen):
    status=requester(base,'/v1/journal/target/'+cid)
    history=status.get('attempts')
    if not isinstance(history,list):fail('CURRENT_HOST_ATTEMPT_INVENTORY_REQUIRED')
    prior=[x for x in history if x.get('attempt')==PRIOR_ATTEMPT]
    if len(prior)!=1 or prior[0].get('terminal') is not False or prior[0].get('last_sequence')!=PRIOR_SEQUENCE or prior[0].get('last_sha256')!=PRIOR_HEAD_SHA or prior[0].get('last_phase')!='FAILED_UNKNOWN':
        fail('EXACT_PRESERVED_PRIOR_FAILED_UNKNOWN_HEAD_REQUIRED')
    unknown=[x for x in history if x.get('terminal') is not True]
    if unknown!=[prior[0]]:fail('ONLY_THE_EXACT_OBSERVED_PRIOR_UNKNOWN_MAY_BE_PRESERVED')
    previous=None;chain=[];last=None
    for sequence in range(PRIOR_SEQUENCE+1):
        with opener(ram.url_base(base)+'/v1/journal/'+PRIOR_ATTEMPT+'/'+format(sequence,'06d'),timeout=120) as response:
            raw=response.read(4*1024**2+1)
        if len(raw)>4*1024**2:fail('BOUNDED_PRIOR_JOURNAL_EVENT_REQUIRED')
        event=json.loads(raw.decode('utf-8'))
        if (event.get('schema')!='eeg.zu3eg.ram_journal_event.v1' or event.get('attempt')!=PRIOR_ATTEMPT or
            event.get('sequence')!=sequence or event.get('prev_sha256')!=previous or event.get('target_cid')!=cid):
            fail('PRIOR_JOURNAL_FULL_CHAIN_OR_TARGET_MISMATCH')
        previous=ram.digest(raw);chain.append(previous);last=event
    if previous!=PRIOR_HEAD_SHA:fail('PRIOR_LOG_BYTES_SHA_MISMATCH')
    receipt=last.get('payload',{});commands=receipt.get('commands',[])
    if last.get('phase')!='FAILED_UNKNOWN' or receipt.get('result')!='FAILED_UNKNOWN' or receipt.get('error')!='INSTALL_COMMAND_FAILED:tar':
        fail('EXACT_GZIP_EXEC_FAILURE_RECEIPT_REQUIRED')
    if len(commands)!=10 or [row.get('argv',[None])[0] for row in commands]!=['sfdisk','blockdev','udevadm','mkfs.vfat','mkfs.ext4','mount','mount','tar','umount','umount']:
        fail('EXACT_PRIOR_COMMAND_BOUNDARY_REQUIRED')
    if any(row.get('returncode')!=0 for i,row in enumerate(commands) if i!=7) or commands[7].get('returncode')!=2 or 'gzip: Cannot exec: No such file or directory' not in commands[7].get('output_tail',''):
        fail('PRIOR_FAILURE_AND_TWO_SUCCESSFUL_UNMOUNTS_REQUIRED')
    original=receipt['plan']
    if original['target']['cid']!=cid or original['target']['bytes']!=EXPECTED['bytes'] or original['environment']['boot_id']!=EXPECTED['ram_boot_id']:
        fail('PRIOR_AUTHORIZED_TARGET_OR_BOOT_MISMATCH')
    if prior[0].get('attempt')!=PRIOR_ATTEMPT:fail('ONLY_THE_EXACT_OBSERVED_PRIOR_UNKNOWN_MAY_BE_PRESERVED')
    return {'attempts':history,'unknown_heads':unknown,
            'prior_head':prior[0],'full_chain_sha256':ram.digest(ram.canonical(chain)),
            'prior_log_sha256':previous,'original_plan':original,'original_consent_sha256':receipt['consent_sha256']}


def parse_mbr(data):
    if len(data)!=512 or data[510:512]!=b'\x55\xaa':fail('EXACT_MBR_SIGNATURE_REQUIRED')
    parts=[]
    for number in range(4):
        row=data[446+number*16:462+number*16]
        parts.append({'number':number+1,'bootable':row[0],'type':row[4],
                      'start':struct.unpack_from('<I',row,8)[0],'sectors':struct.unpack_from('<I',row,12)[0]})
    expected=[{'number':1,'bootable':128,'type':12,'start':EXPECTED['boot_start'],'sectors':EXPECTED['boot_sectors']},
              {'number':2,'bootable':0,'type':131,'start':EXPECTED['root_start'],'sectors':EXPECTED['root_sectors']},
              {'number':3,'bootable':0,'type':0,'start':0,'sectors':0},
              {'number':4,'bootable':0,'type':0,'start':0,'sectors':0}]
    diskid=format(struct.unpack_from('<I',data,440)[0],'08x')
    if diskid!=EXPECTED['disk_id'] or parts!=expected:fail('EXACT_EXISTING_PARTITION_TABLE_REQUIRED_NO_REPARTITION_ALLOWED')
    return {'disk_id':diskid,'partitions':parts,'sector0_sha256':ram.digest(data)}


def process_observation(target):
    result=restore.process_observation(target,exclude_self=True)
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name)==os.getpid():continue
        try:
            words=(process/'cmdline').read_bytes().split(b'\0')
            if b'apply' in words and any(Path(os.fsdecode(x)).name=='eeg_emmc_rootfs_recover.py' for x in words if x):
                result['active_storage_writers'].append({'pid':int(process.name),'tools':['eeg_emmc_rootfs_recover.py']})
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError:result['complete']=False
    return result


def current_target(target):
    rows=[x for x in legacy.mmc_disks() if x['cid']==EXPECTED['cid'] and x['type']=='MMC' and not x['removable']]
    if len(rows)!=1 or rows[0]!=target or ram.target_blockers(target,legacy.read_mounts()):fail('EXACT_TARGET_CHANGED_OR_BUSY')
    observed=process_observation(target)
    if not observed['complete'] or observed['openers'] or observed['active_storage_writers']:fail('CURRENT_NO_LIVE_WRITER_AND_FULL_VISIBILITY_REQUIRED')
    legacy.idle_proof(maintenance_lock_held=True)
    return observed


def root_empty(root):
    root_metadata=root.lstat()
    # ext4 creates the filesystem root as 0755; the installer mountpoint's
    # temporary mode is hidden once the filesystem is mounted.
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_IMODE(root_metadata.st_mode)!=0o755 or root_metadata.st_uid or root_metadata.st_gid:
        fail('REAL_OBSERVED_ROOT_OWNED_0755_EMPTY_FILESYSTEM_ROOT_REQUIRED')
    names=sorted(p.name for p in root.iterdir())
    if names!=['lost+found']:fail('ROOT_MUST_CONTAIN_ONLY_LOST_FOUND_NEVER_OVERLAY_UNKNOWN_CONTENT')
    path=root/'lost+found';metadata=path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode)!=0o700 or metadata.st_uid!=0 or metadata.st_gid!=0 or any(path.iterdir()):
        fail('REAL_EMPTY_ROOT_OWNED_0700_LOST_FOUND_REQUIRED')
    return {'entries':names,'root':{'mode':stat.S_IMODE(root_metadata.st_mode),'uid':root_metadata.st_uid,'gid':root_metadata.st_gid},
            'lost_found':{'mode':stat.S_IMODE(metadata.st_mode),'uid':metadata.st_uid,'gid':metadata.st_gid,'empty':True}}


def boot_inventory(boot,artifacts,manifest_path):
    expected={legacy.ROLES[role]:{'sha256':legacy.sha(artifacts[role]),'bytes':artifacts[role].stat().st_size} for role in ('boot_bin','fit','boot_script')}
    expected['system_payload.manifest.json']={'sha256':legacy.sha(manifest_path),'bytes':manifest_path.stat().st_size}
    if sorted(p.name for p in boot.iterdir())!=sorted(expected):fail('EXACT_FOUR_EXISTING_BOOT_FILES_REQUIRED')
    for name,row in expected.items():
        path=boot/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes'] or legacy.sha(path)!=row['sha256']:
            fail('EXISTING_BOOT_FILE_CHANGED:'+name)
    return expected


def extraction_seam(work,commands):
    """Execute the real production GNU tar→gzip edge in RAM before target RW."""
    if legacy.find_mount(work,legacy.read_mounts())['fstype'] not in ram.RAM_FS:fail('SEAM_FIXTURE_MUST_BE_RAM_ONLY')
    archive=work/'production-seam.tar.gz';destination=work/'seam-root';destination.mkdir()
    with tarfile.open(archive,'w:gz',format=tarfile.PAX_FORMAT) as output:
        for name,kind in (('content',tarfile.REGTYPE),('relative-link',tarfile.SYMTYPE),('same-inode',tarfile.LNKTYPE)):
            member=tarfile.TarInfo(name);member.uid=member.gid=0;member.mode=0o640;member.type=kind
            if kind==tarfile.REGTYPE:
                data=b'EEG_REAL_GNU_TAR_GZIP_EXEC_SEAM\n';member.size=len(data);output.addfile(member,io.BytesIO(data))
            else:member.linkname='content';output.addfile(member)
    checked_command(['tar','--numeric-owner','--xattrs','--acls','-xpf',str(archive),'-C',str(destination)],commands)
    content=destination/'content';metadata=content.stat()
    if (content.read_bytes()!=b'EEG_REAL_GNU_TAR_GZIP_EXEC_SEAM\n' or stat.S_IMODE(metadata.st_mode)!=0o640 or metadata.st_uid or metadata.st_gid or
        not (destination/'relative-link').is_symlink() or os.readlink(destination/'relative-link')!='content' or
        (destination/'same-inode').stat().st_ino!=metadata.st_ino):fail('REAL_EXTRACTED_DATA_OWNER_MODE_AND_LINKS_REQUIRED')
    tools={}
    for name in ('tar','gzip'):
        path=shutil.which(name)
        if path is None:fail('EXECUTED_GZIP_PATH_BINDING_REQUIRED')
        resolved=Path(path).resolve(strict=True)
        tools[name]={'path':path,'resolved':str(resolved),'sha256':legacy.sha(resolved)}
    return {'result':'PASS_REAL_PRODUCTION_TAR_GZIP_EXEC','tools':tools,'fixture_sha256':legacy.sha(archive)}


def observe_storage(target,artifacts,manifest_path,work,commands):
    current_target(target)
    with restore.open_device(target,False) as stream:mbr=parse_mbr(restore.read_exact(stream,512))
    if len(target['partitions'])!=2 or target['partitions'][0]['bytes']!=EXPECTED['boot_sectors']*512 or target['partitions'][1]['bytes']!=EXPECTED['root_sectors']*512:
        fail('SYSFS_PARTITION_CAPACITY_MISMATCH')
    device=target['device'];bootpart,rootpart=Path(device+'p1'),Path(device+'p2')
    values={}
    for part,name in ((rootpart,'root'),(bootpart,'boot')):
        values[name+'_uuid']=checked_command(['blkid','-c','/dev/null','-s','UUID','-o','value',str(part)],commands)
        values[name+'_partuuid']=checked_command(['blkid','-c','/dev/null','-s','PARTUUID','-o','value',str(part)],commands).lower()
    if values!={'root_uuid':EXPECTED['root_uuid'],'boot_uuid':EXPECTED['boot_uuid'],
                'root_partuuid':EXPECTED['disk_id']+'-02','boot_partuuid':EXPECTED['disk_id']+'-01'}:
        fail('EXACT_PRESERVED_FILESYSTEM_UUIDS_REQUIRED')
    # All fsck return codes must be zero; no repair flag exists here.
    checked_command(['fsck.fat','-n',str(bootpart)],commands)
    checked_command(['e2fsck','-f','-n',str(rootpart)],commands)
    root,boot=work/'observed-root',work/'observed-boot';root.mkdir();boot.mkdir();mounted=[]
    try:
        checked_command(['mount','-t','ext4','-o','ro,noload',str(rootpart),str(root)],commands);mounted.append(root)
        empty=root_empty(root)
        checked_command(['umount',str(root)],commands);mounted.remove(root)
        checked_command(['mount','-t','vfat','-o','ro',str(bootpart),str(boot)],commands);mounted.append(boot)
        inventory=boot_inventory(boot,artifacts,manifest_path)
        checked_command(['umount',str(boot)],commands);mounted.remove(boot)
    finally:
        for path in reversed(mounted):checked_command(['umount',str(path)],commands)
    current_target(target)
    return dict(values,mbr=mbr,empty_root=empty,boot_files=inventory,fsck_fat_rc=0,e2fsck_rc=0,read_only_mounts_unmounted=True)


def preserved_marker():
    marker=LOCK.with_name('maintenance.active');ram.no_symlink_components(marker)
    if not marker.is_file() or marker.read_bytes()!=('emmc-ram-install '+PRIOR_ATTEMPT+'\n').encode():fail('EXACT_OLD_MAINTENANCE_MARKER_MUST_REMAIN')
    return {'sha256':legacy.sha(marker),'bytes':marker.stat().st_size,'content':'emmc-ram-install '+PRIOR_ATTEMPT}


def remove_empty_workspace(work):
    """Remove this tool's small RAM-only probe files; never recurse or follow mounts."""
    work=Path(work)
    mounted={row['mountpoint'] for row in legacy.read_mounts()}
    work_name=work.as_posix()
    boundary=work_name.rstrip('/')+'/'
    if any(point==work_name or point.startswith(boundary) for point in mounted):
        return {'removed':False,'reason':'TASK_MOUNT_STILL_PRESENT','path':str(work)}
    allowed={'production-seam.tar.gz','seam-root','observed-root','observed-boot','root','boot'}
    entries=list(work.iterdir())
    if any(path.name not in allowed or path.is_symlink() for path in entries):
        return {'removed':False,'reason':'UNEXPECTED_WORKSPACE_ENTRY_PRESERVED','path':str(work)}
    removed=[]
    for path in entries:
        if path.is_file():
            size=path.stat().st_size;sha=legacy.sha(path);path.unlink()
            removed.append({'path':str(path),'bytes':size,'sha256':sha})
        elif path.is_dir():
            children=list(path.iterdir())
            if any((child.is_symlink() and (path.name!='seam-root' or child.name!='relative-link' or os.readlink(child)!='content'))
                   or (not child.is_symlink() and not child.is_file()) for child in children):
                return {'removed':False,'reason':'NONFLAT_WORKSPACE_TREE_PRESERVED','path':str(work)}
            for child in children:
                if child.is_symlink():
                    link=os.readlink(child);data=link.encode('utf-8');child.unlink()
                    removed.append({'path':str(child),'type':'symlink','link':link,'bytes':len(data),'sha256':ram.digest(data)})
                else:
                    size=child.stat().st_size;sha=legacy.sha(child);child.unlink()
                    removed.append({'path':str(child),'bytes':size,'sha256':sha})
            path.rmdir()
        else:return {'removed':False,'reason':'UNEXPECTED_WORKSPACE_TYPE_PRESERVED','path':str(work)}
    work.rmdir()
    return {'removed':True,'files':removed,'logical_bytes':sum(row['bytes'] for row in removed),'workspace':str(work)}


def validate_evidence(value,tool_sha,environment,prior):
    expected={'schema':'eeg.zu3eg.rootfs_only_recovery_evidence.v1','prior_attempt':PRIOR_ATTEMPT,
              'prior_log_sha256':PRIOR_HEAD_SHA,'initial_observation_sha256':INITIAL_OBSERVATION_SHA,
              'target_cid':EXPECTED['cid'],'ram_boot_id':EXPECTED['ram_boot_id'],
              'scope':'EXISTING_EXT4_ROOT_CONTENT_AND_FSTAB_ONLY','tool_sha256':tool_sha}
    if not isinstance(value,dict) or any(value.get(k)!=v for k,v in expected.items()):fail('EXACT_ROOTFS_RECOVERY_SOURCE_EVIDENCE_REQUIRED')
    if not ram.valid_sha(value.get('current_post_fat_repair_observation_sha256')) or value['current_post_fat_repair_observation_sha256']==INITIAL_OBSERVATION_SHA:
        fail('NEW_FAT_FSCK_ZERO_READ_ONLY_OBSERVATION_RECEIPT_REQUIRED')
    if environment['boot_id']!=EXPECTED['ram_boot_id'] or prior['original_plan']['environment']['sha256']!=environment['sha256']:
        fail('SAME_ORIGINALLY_AUTHORIZED_RAM_BOOT_ENVIRONMENT_REQUIRED')


def build_plan(args,lock_held=False):
    if os.geteuid()!=0:fail('ROOT_REQUIRED_FOR_READONLY_MOUNT_OBSERVATION')
    if args.tool_sha256!=legacy.sha(Path(__file__)):fail('PINNED_NEW_RECOVERY_SOURCE_REQUIRED')
    evidence=legacy.json_read(args.evidence);commands=[]
    plan,manifest,artifacts=ram.build_plan(args.manifest,args.environment,ram.require_cid(args.target_cid),maintenance_lock_held=lock_held)
    if plan['target'] is None or plan['target']['cid']!=EXPECTED['cid'] or plan['target']['bytes']!=EXPECTED['bytes']:
        fail('EXACT_PREVIOUSLY_AUTHORIZED_EMMC_REQUIRED')
    prior=prior_journal(args.host_url,args.target_cid)
    validate_evidence(evidence,args.tool_sha256,plan['environment'],prior)
    marker=preserved_marker();observed=process_observation(plan['target'])
    if not observed['complete'] or observed['openers'] or observed['active_storage_writers']:fail('NO_LIVE_WRITER_REQUIRED_FOR_RECOVERY')
    work=Path(tempfile.mkdtemp(prefix='eeg-rootfs-recovery-plan-',dir='/run'))
    seam=None;storage=None;blockers=list(plan['blockers'])
    try:
        seam=extraction_seam(work,commands)
        storage=observe_storage(plan['target'],artifacts,args.manifest,work,commands)
    except (RuntimeError,OSError,ValueError) as exc:blockers.append(str(exc))
    cleanup=remove_empty_workspace(work)
    if not cleanup.get('removed'):blockers.append('RAM_PLAN_WORKSPACE_CLEANUP_INCOMPLETE:'+cleanup.get('reason','UNKNOWN'))
    result={'schema':'eeg.zu3eg.rootfs_only_recovery_plan.v1','revision':REVISION,'tool_sha256':args.tool_sha256,
            'environment':plan['environment'],'target':plan['target'],'source_plan':plan,'evidence':evidence,
            'evidence_sha256':legacy.sha(args.evidence),'prior_host_attempts':prior['attempts'],'prior_unknown_heads':prior['unknown_heads'],
            'prior_full_chain_sha256':prior['full_chain_sha256'],'prior_log_sha256':prior['prior_log_sha256'],
            'original_consent_sha256':prior['original_consent_sha256'],'preserved_maintenance_marker':marker,
            'process_observation':observed,'storage_observation':storage,'production_seam':seam,'read_only_commands':commands,
            'plan_workspace_cleanup':cleanup,
            'overwrite':{'area':'existing_ext4_root_partition_only','offset':EXPECTED['root_start']*512,'bytes':EXPECTED['root_sectors']*512},
            'excluded_from_writes':['partition_table','FAT_BOOT','boot0','boot1','RPMB','EXT_CSD','QSPI'],
            'blockers':blockers,'software_plan_clear':not blockers,'can_apply':False}
    # Exclude timestamped diagnostic command log / ephemeral fixture paths from
    # the decision digest. Their content remains in the returned evidence.
    # Diagnostic commands and the RAM-only workspace contain timestamps or
    # random temporary paths. They remain in the evidence, but must not make a
    # repeated apply-time read-only plan differ from the operator-approved plan.
    binding={k:v for k,v in result.items() if k not in ('read_only_commands','production_seam','plan_workspace_cleanup')}
    binding['production_seam_tools']=None if seam is None else seam['tools']
    result['plan_sha256']=ram.digest(ram.canonical(binding))
    return result,manifest,artifacts


def validate_consent(record,plan,attempt):
    restore.ident(attempt)
    expected={'schema':'eeg.zu3eg.rootfs_only_recovery_consent.v1','attempt':attempt,'decision':'CONTINUE_ORIGINAL_APPROVED_INSTALL_ROOTFS_ONLY',
              'target_cid':EXPECTED['cid'],'ram_boot_id':EXPECTED['ram_boot_id'],'plan_sha256':plan['plan_sha256'],
              'evidence_sha256':plan['evidence_sha256'],'prior_log_sha256':PRIOR_HEAD_SHA,
              'prior_unknown_heads':plan['prior_unknown_heads'],'preserved_marker_sha256':plan['preserved_maintenance_marker']['sha256'],
              'original_operator_consent_sha256':plan['original_consent_sha256'],
              'overwrite':plan['overwrite'],'recovery_observation_sha256':ram.digest(ram.canonical(plan['storage_observation']))}
    confirmation=('CONTINUE ROOTFS ONLY '+EXPECTED['cid']+'; preserve UNKNOWN '+PRIOR_ATTEMPT)
    if (not isinstance(record,dict) or any(record.get(k)!=v for k,v in expected.items()) or
        record.get('decision')!='CONTINUE_ORIGINAL_APPROVED_INSTALL_ROOTFS_ONLY' or
        record.get('operator_confirmation_text')!=confirmation or
        record.get('continuation_within_existing_overwrite_authorization') is not True):
        fail('EXACT_CURRENT_ROOTFS_ONLY_CONTINUATION_CONSENT_REQUIRED')
    if any(row.get('attempt')==attempt for row in plan['prior_host_attempts']):fail('NEW_SINGLE_USE_RECOVERY_ATTEMPT_REQUIRED_NEVER_REPLAY')
    if plan['storage_observation'].get('fsck_fat_rc')!=0 or plan['storage_observation'].get('e2fsck_rc')!=0:
        fail('ALL_READ_ONLY_FILESYSTEM_CHECKS_MUST_BE_ZERO')
    if plan['production_seam'].get('result')!='PASS_REAL_PRODUCTION_TAR_GZIP_EXEC':
        fail('ACTUAL_GNU_TAR_GZIP_PRODUCTION_SEAM_REQUIRED')
    return record


def recovery_worker(plan,manifest,artifacts,manifest_path,journal,work,receipt,guard=current_target):
    target=plan['target'];guard(target);commands=receipt['commands']
    root=work/'root';root.mkdir();mounted=[]
    rootpart=Path(target['device']+'p2');bootpart=Path(target['device']+'p1')
    command=lambda argv:checked_command(argv,commands,journal)
    try:
        # Re-read observed-empty condition immediately before the first RW mount.
        command(['mount','-t','ext4','-o','ro,noload',str(rootpart),str(root)]);mounted.append(root)
        if root_empty(root)!=plan['storage_observation']['empty_root']:fail('EMPTY_ROOT_CHANGED_BEFORE_RECOVERY')
        command(['umount',str(root)]);mounted.clear()
        journal.event('BEFORE_ROOTFS_RW','mount_existing_root_no_format',{'root_uuid':EXPECTED['root_uuid'],'root_partuuid':EXPECTED['disk_id']+'-02'})
        receipt['target_write_may_have_started']=True
        command(['mount','-t','ext4',str(rootpart),str(root)]);mounted.append(root)
        command(['tar','--numeric-owner','--xattrs','--acls','-xpf',str(artifacts['rootfs_tar']),'-C',str(root)])
        receipt['rootfs_readback']=legacy.verify_installed_rootfs(artifacts['rootfs_tar'],root)
        legacy.identity_equal(legacy.json_read(root/'etc/eeg-system/baseline.json'),manifest['system_identity'])
        legacy.verify_required_installed_paths(root,manifest['rootfs']['required_paths'],manifest['rootfs']['init_system'])
        before=root.stat()
        if (stat.S_IMODE(before.st_mode),before.st_uid,before.st_gid)!=(0o755,0,0):fail('ROOT_DIRECTORY_MODE_OWNER_CHANGED_DURING_TAR')
        journal.event('BEFORE_ROOT_DIRECTORY_MODE_NORMALIZATION','set_existing_root_mountpoint_0755',
                      {'before_mode':0o755,'after_mode':0o755,'uid':0,'gid':0,'archive_contains_root_directory_member':False})
        root.chmod(0o755)
        after=root.stat()
        if (stat.S_IMODE(after.st_mode),after.st_uid,after.st_gid)!=(0o755,0,0):fail('ROOT_DIRECTORY_MODE_0755_READBACK_REQUIRED')
        receipt['root_directory_mode']={'before_mode':0o755,'after_mode':0o755,'uid':0,'gid':0,
                                        'source':'explicit root mountpoint metadata normalization; archive has no root entry'}
        fstab=root/'etc/fstab';preserved=[]
        for line in fstab.read_text().splitlines():
            fields=line.split()
            if fields and not fields[0].startswith('#') and len(fields)>=2 and fields[1] in ('/','/boot'):continue
            preserved.append(line)
        journal.event('BEFORE_FSTAB','write_same_filesystem_uuid_fstab',{'root_uuid':EXPECTED['root_uuid'],'boot_uuid':EXPECTED['boot_uuid']})
        fstab.write_text('\n'.join(preserved+['UUID='+EXPECTED['root_uuid']+' / ext4 defaults,noatime 0 1',
                                            'UUID='+EXPECTED['boot_uuid']+' /boot vfat defaults,nofail,umask=0077 0 2'])+'\n')
        free=os.statvfs(root).f_bavail*os.statvfs(root).f_frsize
        if free<manifest['layout']['root_min_free_bytes']:fail('COMPLETE_ROOTFS_RETAINS_INSUFFICIENT_DEVELOPMENT_SPACE')
        receipt['installed']={'root_uuid':EXPECTED['root_uuid'],'boot_uuid':EXPECTED['boot_uuid'],'root_partuuid':EXPECTED['disk_id']+'-02',
                              'root_free_bytes':free,'fstab_sha256':legacy.sha(fstab),'boot_partitions_modified':False,
                              'boot_source_after_install':'UNVERIFIED_UNTIL_EMMC_BOOT'}
        receipt['persistent_config_preserved']=[]
        command(['sync']);command(['umount',str(root)]);mounted.clear()
        command(['e2fsck','-f','-n',str(rootpart)]);command(['fsck.fat','-n',str(bootpart)])
        command(['mount','-t','ext4','-o','ro,noload',str(rootpart),str(root)]);mounted.append(root)
        receipt['rootfs_readback_after_flush']=legacy.verify_installed_rootfs(artifacts['rootfs_tar'],root,{'etc/fstab'})
        if legacy.sha(legacy.installed_path(root,'etc/fstab'))!=receipt['installed']['fstab_sha256']:fail('FSTAB_POST_FLUSH_READBACK_MISMATCH')
        legacy.identity_equal(legacy.json_read(root/'etc/eeg-system/baseline.json'),manifest['system_identity'])
        legacy.verify_required_installed_paths(root,manifest['rootfs']['required_paths'],manifest['rootfs']['init_system'])
        command(['umount',str(root)]);mounted.clear()
        boot=work/'boot';boot.mkdir();command(['mount','-t','vfat','-o','ro',str(bootpart),str(boot)]);mounted.append(boot)
        if boot_inventory(boot,artifacts,manifest_path)!=plan['storage_observation']['boot_files']:fail('BOOT_CHANGED_DURING_ROOTFS_ONLY_RECOVERY')
        command(['umount',str(boot)]);mounted.clear();guard(target)
        with restore.open_device(target,False) as stream:after=parse_mbr(restore.read_exact(stream,512))
        if after!=plan['storage_observation']['mbr']:fail('MBR_CHANGED_DURING_ROOTFS_ONLY_RECOVERY')
        if preserved_marker()!=plan['preserved_maintenance_marker']:fail('OLD_UNKNOWN_MARKER_CHANGED')
        receipt['result']=ram.TERMINAL_PHASE
    finally:
        for path in reversed(mounted):
            try:command(['umount',str(path)])
            except Exception as exc:receipt.setdefault('cleanup_errors',[]).append(str(exc))


def apply(args):
    import fcntl
    if args.confirm_rootfs!=EXPECTED['cid'] or args.consent is None:fail('EXACT_CID_AND_CONTINUATION_CONSENT_REQUIRED')
    restore.ident(args.attempt);ram.no_symlink_components(LOCK)
    receipt={'schema':'eeg.zu3eg.rootfs_only_recovery_receipt.v1','revision':REVISION,'attempt':args.attempt,
             'result':'INCOMPLETE','commands':[],'target_write_may_have_started':False,'board_boot_verification':'NOT_EXERCISED'}
    journal=ram.HostJournal(args.host_url,args.attempt,EXPECTED['cid'])
    with LOCK.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            plan,manifest,artifacts=build_plan(args,True)
            if plan['blockers']:fail('ROOTFS_RECOVERY_PLAN_BLOCKED:'+','.join(plan['blockers']))
            record=validate_consent(legacy.json_read(args.consent),plan,args.attempt)
            receipt['plan']=plan;receipt['consent_sha256']=legacy.sha(args.consent)
            journal.event('AUTHORIZED','observed_empty_rootfs_only_continuation',{'plan_sha256':plan['plan_sha256'],'consent_sha256':receipt['consent_sha256'],
                'pid':os.getpid(),'boot_id':EXPECTED['ram_boot_id'],'preserved_prior_unknown_heads':plan['prior_unknown_heads'],
                'preserved_prior_marker_sha256':plan['preserved_maintenance_marker']['sha256'],'prior_log_sha256':PRIOR_HEAD_SHA})
            work=Path(tempfile.mkdtemp(prefix='eeg-rootfs-recovery-write-',dir='/run'))
            recovery_worker(plan,manifest,artifacts,args.manifest,journal,work,receipt)
            if receipt.get('cleanup_errors'):fail('RECOVERY_CLEANUP_INCOMPLETE_OBSERVE_NO_REPEAT')
            receipt['workspace_cleanup']=remove_empty_workspace(work)
            if not receipt['workspace_cleanup'].get('removed'):fail('RECOVERY_RAM_WORKSPACE_CLEANUP_INCOMPLETE')
            journal.event(ram.TERMINAL_PHASE,'rootfs_recovery_readback_boot_pending',receipt)
            # Historical marker and FAILED_UNKNOWN attempt remain unchanged.
        except Exception as exc:
            receipt['error']=str(exc);receipt['result']='FAILED_UNKNOWN' if receipt['target_write_may_have_started'] else 'REJECTED_BEFORE_WRITE'
            if 'work' in locals():receipt['workspace_cleanup']=remove_empty_workspace(work)
            with contextlib.suppress(Exception):journal.event(receipt['result'],'preserve_prior_unknown_and_observe',receipt)
            raise
        finally:print(json.dumps(receipt,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','apply'),nargs='?',default='plan')
    parser.add_argument('--manifest',type=Path,default=ram.CACHE/'system_payload.manifest.json')
    parser.add_argument('--environment',type=Path,default=ram.ENVIRONMENT)
    parser.add_argument('--host-url',required=True);parser.add_argument('--target-cid',required=True)
    parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--tool-sha256',required=True)
    parser.add_argument('--attempt',default=uuid.uuid4().hex);parser.add_argument('--consent',type=Path);parser.add_argument('--confirm-rootfs')
    args=parser.parse_args()
    if args.action=='plan':
        plan,_,_=build_plan(args);print(json.dumps(plan,ensure_ascii=False,indent=2));return 0 if plan['software_plan_clear'] else 3
    apply(args);return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print('EEG_ROOTFS_ONLY_RECOVERY_REJECTED_OR_UNKNOWN: '+str(exc),file=sys.stderr);sys.exit(3)
