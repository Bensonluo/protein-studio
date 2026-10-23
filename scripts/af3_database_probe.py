#!/usr/bin/env python3
"""Offline preflight for the pinned AF3 v3.0.4 CPU database installation.

Never downloads data or accesses a GPU. --sample-input explicitly runs a real
CPU MSA/template job and may take substantial time; no sample is run by default.
Preflight is not a full-database checksum or proof of prediction accuracy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.databases import AF3_ASSETS, AF3_TOOLS, get_profile
from backend.staged import _validate_af3
from scripts.af3_staged_runner import build_command

MIN_MMCIF_RECORDS = 100000  # Conservative sanity floor, not an official record count.
AF3_COMMIT = '85c4d20505fd5cef05eac22b534d4e793971ae69'


def _run(argv, timeout=30, log_path=None):
    """Bound output memory and terminate the complete CPU subprocess group."""
    temporary = None
    if log_path is None:
        temporary = tempfile.TemporaryDirectory(prefix='af3-probe-')
        log_path = Path(temporary.name)/'command.log'
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(CUDA_VISIBLE_DEVICES='', JAX_PLATFORMS='cpu')
    started = time.monotonic()
    try:
        with log_path.open('wb') as output:
            process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=(os.name=='posix'))
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name=='posix': os.killpg(process.pid, signal.SIGTERM)
                else: process.terminate()
                try: process.wait(5)
                except subprocess.TimeoutExpired:
                    if os.name=='posix': os.killpg(process.pid, signal.SIGKILL)
                    else: process.kill()
                    process.wait()
                raise TimeoutError(f'CPU probe command exceeded {timeout} seconds; process group stopped.')
        with log_path.open('rb') as source:
            text=source.read(128*1024).decode('utf-8', errors='replace')
        return dict(returncode=code,output=text,outputTruncated=log_path.stat().st_size>128*1024,
                    seconds=round(time.monotonic()-started,3))
    finally:
        if temporary is not None: temporary.cleanup()


def _file_evidence(path):
    path=Path(path)
    size=path.stat().st_size
    with path.open('rb') as stream:
        head=stream.read(4096)
        stream.seek(max(0,size-4096))
        tail=stream.read(4096)
    if b'\0' in head+tail:
        raise ValueError('NUL bytes in FASTA sample; possible sparse dummy file or incomplete output.')
    lines=[line.strip() for line in tail.decode('ascii').splitlines() if line.strip()]
    if not lines or not re.fullmatch('[A-Za-z*.-]+',lines[-1]):
        raise ValueError('FASTA tail is not a sequence; inspect interrupted decompression or wrong content.')
    return dict(bytes=size,mtimeNs=path.stat().st_mtime_ns,
                headSha256=hashlib.sha256(head).hexdigest(),tailSha256=hashlib.sha256(tail).hexdigest(),
                integrity='Only 4 KiB head/tail fingerprints; not a whole-file checksum.')


def probe(config, profile_id, alphafold_script, python=sys.executable,
          sample_input=None, sample_output=None, sample_timeout=86400):
    report=dict(schemaVersion=1,pinnedVersion='v3.0.4',pinnedCommit=AF3_COMMIT,checkedAt=datetime.now(timezone.utc).isoformat(),
                ready=False,diagnostics=[],tools={},assets={},samplePreparation={'status':'not-run'},
                scope='Local CPU preparation preflight. No database download, GPU inference, cloud capacity, or full-database integrity claim.')
    def issue(code,message,severity='error'):
        report['diagnostics'].append(dict(code=code,message=str(message),severity=severity))
    try:
        profile=get_profile(profile_id,config)
    except (OSError,ValueError,KeyError) as exc:
        issue('registry',exc)
        return report
    report.update(profileId=profile_id,profileFingerprint=profile['profileFingerprint'],
                  release=profile['release'],prepareVersion=profile['prepareVersion'],searchVersion=profile['searchVersion'])
    report['diagnostics'].extend(profile['diagnostics'])
    if profile.get('validationProfile')!='af3-v3.0.4':
        issue('strict-profile-required','Set validationProfile=af3-v3.0.4; a generic or tiny test inventory does not qualify as this deployment.')
    for key in AF3_ASSETS:
        path=profile['paths'].get(key)
        if not path: continue
        try:
            if key=='pdb_database_path':
                count=0
                with os.scandir(path) as entries:
                    for entry in entries:
                        if re.fullmatch('[a-z0-9]{4,}\\.cif',entry.name) and entry.is_file(): count+=1
                report['assets'][key]=dict(flatMmcifRecords=count,minimumSanityCount=MIN_MMCIF_RECORDS,
                    integrity='Filename count plus registry header samples; not exhaustive CIF validation.')
                if count<MIN_MMCIF_RECORDS:
                    issue('incomplete-mmcif',f'Only {count} flat mmCIF records; below the {MIN_MMCIF_RECORDS} conservative sanity floor for the official snapshot.')
            else:
                report['assets'][key]=_file_evidence(path)
        except (OSError,UnicodeError,ValueError) as exc:
            issue('asset-content',f'{key}: {exc}')
    for key in AF3_TOOLS:
        path=profile.get('executables',{}).get(key)
        if not path:
            issue('tool-missing',key)
            continue
        try:
            result=_run([path,'-h'],timeout=15)
            match=re.search(r'\bHMMER\s+(\d+\.\d+(?:\.\d+)?)\b',result['output'])
            record=dict(path=path,version=match.group(1) if match else None,returncode=result['returncode'],
                        helpSha256=hashlib.sha256(result['output'].encode()).hexdigest())
            report['tools'][key]=record
            if result['returncode']!=0 or record['version']!='3.4':
                issue('tool-version',f'{key}: expected working HMMER 3.4, observed {record["version"]!r}, exit {result["returncode"]}.')
            if key=='jackhmmer_binary_path':
                record['seqLimitSupported']='--seq_limit' in result['output']
                if not record['seqLimitSupported']:
                    issue('jackhmmer-unpatched','Official AF3 can fall back without --seq_limit; the official 3.4 patch is recommended to bound redundant-hit output and peak memory.','warning')
        except (OSError,TimeoutError) as exc:
            issue('tool-execution',f'{key}: {exc}')
    script=Path(alphafold_script).expanduser().resolve()
    try:
        if not script.is_file(): raise ValueError('run_alphafold.py is missing.')
        revision=_run(['git','-C',str(script.parent),'rev-parse','HEAD'])
        pinned=_run(['git','-C',str(script.parent),'rev-parse','refs/tags/v3.0.4^{commit}'])
        changes=_run(['git','-C',str(script.parent),'status','--porcelain','--untracked-files=no'])
        head=revision['output'].strip()
        report['source']=dict(script=str(script),commit=head,tagCommit=pinned['output'].strip(),
                              trackedChanges=changes['output'].strip())
        if (revision['returncode'] or pinned['returncode'] or changes['returncode'] or
            head!=AF3_COMMIT or head!=pinned['output'].strip() or changes['output'].strip()):
            issue('source-version','Use a clean checkout at the v3.0.4 tag. The probe requires its local Git revision evidence, including inside a container.')
        help_result=_run([python,str(script),'--helpshort'],timeout=120)
        required=('--run_data_pipeline','--run_inference','--compress_large_output_files','--json_path')
        report['runtime']=dict(python=python,helpReturnCode=help_result['returncode'],
                               flagsPresent={flag:bool(re.search(r'--(?:\[no\])?'+re.escape(flag[2:])+r'\b',help_result['output'])) for flag in required})
        if help_result['returncode'] not in (0,1) or not all(report['runtime']['flagsPresent'].values()):
            report['runtime']['diagnosticOutput']=help_result['output'][-4000:]
            issue('runtime-import','AF3 CLI import/help failed or required stage flags are absent. Check the installed Python dependency environment.')
    except (OSError,TimeoutError,ValueError) as exc:
        issue('runtime-source',exc)
    preliminary=not any(d['severity']=='error' for d in report['diagnostics'])
    if sample_input is not None:
        if not preliminary:
            report['samplePreparation']={'status':'blocked','reason':'Fix preflight errors before launching database search.'}
        else:
            try:
                if not sample_output: raise ValueError('--sample-output is required with --sample-input.')
                output=Path(sample_output).expanduser().resolve()
                output.mkdir(parents=True,exist_ok=False)
                source=Path(sample_input).expanduser().resolve()
                data=json.loads(source.read_text())
                chains=[]
                for entry in data['sequences']:
                    if set(entry)!={'protein'}: raise ValueError('This sample probe matches the current protein-only platform input contract.')
                    protein=entry['protein']
                    ids=protein['id'] if isinstance(protein['id'],list) else [protein['id']]
                    chains.extend(dict(id=cid,sequence=protein['sequence']) for cid in ids)
                    if any(protein.get(field) is not None for field in ('unpairedMsa','pairedMsa','templates','unpairedMsaPath','pairedMsaPath')):
                        raise ValueError('A genuine database smoke input must leave MSA/templates unset; prefilled/empty fields would skip search.')
                if not chains: raise ValueError('Sample needs at least one protein chain.')
                profile_path=output/'database-profile.json'
                profile_path.write_text(json.dumps(profile))
                args=argparse.Namespace(stage='prepare',input=str(source),output_dir=str(output/'prepared'),
                    profile_json=str(profile_path),job_json=None,alphafold_script=str(script),python=python)
                command=build_command(args,[])
                result=_run(command,timeout=sample_timeout,log_path=output/'prepare.log')
                if result['returncode']!=0: raise ValueError(f'CPU preparation exited {result["returncode"]}; inspect {output/"prepare.log"}.')
                candidates=list((output/'prepared').rglob('*_data.json'))
                if len(candidates)!=1: raise ValueError('CPU preparation did not produce exactly one uncompressed *_data.json.')
                prepared=_validate_af3(candidates[0],chains)
                report['samplePreparation']=dict(status='passed',seconds=result['seconds'],output=str(candidates[0]),
                    sha256=hashlib.sha256(candidates[0].read_bytes()).hexdigest(),
                    chains=[dict(ids=p['protein']['id'],unpairedMsaRows=p['protein']['unpairedMsa'].count('>'),
                                 pairedMsaRows=p['protein']['pairedMsa'].count('>'),templates=len(p['protein']['templates'])) for p in prepared['sequences']],
                    meaning='Real CPU MSA/template command completed with self-contained prepared input; GPU prediction not run.')
            except (OSError,TimeoutError,ValueError,KeyError,TypeError) as exc:
                issue('sample-preparation',exc)
                report['samplePreparation']={'status':'failed','error':str(exc)}
    report['ready']=not any(d['severity']=='error' for d in report['diagnostics'])
    report['readinessLevel']='cpu-preparation-smoke-passed' if report['ready'] and sample_input else 'preflight-passed' if report['ready'] else 'blocked'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--alphafold-script',required=True)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--output',help='Save a timestamped evidence report at this path, as well as stdout.')
    parser.add_argument('--sample-input',help='Explicitly execute CPU database search for one protein JSON.')
    parser.add_argument('--sample-output',help='Fresh output directory; existing directories are rejected.')
    parser.add_argument('--sample-timeout',type=int,default=86400)
    args=parser.parse_args()
    if not 1<=args.sample_timeout<=7*86400: parser.error('--sample-timeout must be 1..604800 seconds.')
    report=probe(args.config,args.profile,args.alphafold_script,args.python,args.sample_input,args.sample_output,args.sample_timeout)
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        path=Path(args.output).expanduser()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text+'\n')
    print(text)
    return 0 if report['ready'] else 2


if __name__=='__main__': raise SystemExit(main())
