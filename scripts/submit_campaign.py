#!/usr/bin/env python3
"""Stream FASTA or multi-chain JSONL targets into a persistent staged campaign.

Each candidate remains a separate prediction. --fixed-target combines each
candidate FASTA record with the same fixed FASTA chains. Memory is bounded by
one record and at most 100 pending targets, not the candidate-file size.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from .submit_batch import normalize_server, parse_seeds
    from .screening_input import (target_id, detect_format, load_fixed_target, iter_targets,
                                  residue_limit, canonical_chains_sha, validate_item_settings,
                                  FIXED_STRATEGY, INDEPENDENT_STRATEGY)
except ImportError:
    from submit_batch import normalize_server, parse_seeds
    from screening_input import (target_id, detect_format, load_fixed_target, iter_targets,
                                 residue_limit, canonical_chains_sha, validate_item_settings,
                                 FIXED_STRATEGY, INDEPENDENT_STRATEGY)


def request_json(server, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if data is not None and len(data) > 12 * 1024 * 1024:
        raise ValueError('Chunk exceeds 12 MiB. Reduce --chunk-size.')
    request = Request(server + path, data=data, method='GET' if data is None else 'POST',
                      headers={'Content-Type':'application/json'} if data is not None else {})
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {exc.read(4096).decode("utf-8",errors="replace")}') from exc
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'HTTP outcome unknown; rerun the same file with --campaign-id to replay idempotent target IDs. {exc}') from exc


def execution_profile(value):
    try:
        data=json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError('Execution profile must be JSON such as {"id":"local","version":"1"}.') from exc
    if not isinstance(data,dict) or set(data)-{'id','version'} or any(not isinstance(v,str) for v in data.values()):
        raise argparse.ArgumentTypeError('Execution profile accepts only string id/version metadata; commands belong in administrator configuration.')
    return data


def file_sha(path):
    value=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def manifest_writer(path, resume, expected):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+',encoding='utf-8') as handle:
        try:
            import fcntl
        except ImportError:
            fcntl=None
        if fcntl is not None:
            try: fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc: raise ValueError('Another submitter owns this manifest.') from exc
        try:
            handle.seek(0)
            has_identity = False
            for number,line in enumerate(handle,1):
                if not line.strip(): continue
                if not resume:
                    raise ValueError('Manifest already exists. Use --campaign-id to resume, or choose a new --manifest for a new campaign.')
                try: item=json.loads(line)
                except json.JSONDecodeError as exc: raise ValueError(f'Manifest line {number} is invalid; repair it before resuming.') from exc
                if not isinstance(item, dict):
                    raise ValueError(f'Manifest line {number} is not an object.')
                if item.get('event') in ('created','attached'):
                    has_identity = True
                    for key in ('server','inputSha256','campaignId','namePrefix'):
                        if item.get(key)!=expected.get(key):
                            raise ValueError(f'Manifest {key} differs; preserve the original input/configuration when resuming.')
                    legacy = 'constructionStrategy' not in item
                    if legacy:
                        if expected.get('constructionStrategy') != INDEPENDENT_STRATEGY or expected.get('inputFormat') != 'fasta':
                            raise ValueError('Legacy manifest only supports the original independent FASTA input; fixed-target/JSONL identity cannot be verified.')
                    else:
                        for key in ('inputFormat','constructionStrategy','chainIdPolicy','fixedTargetSha256','fixedTargetChainsSha256'):
                            if item.get(key) != expected.get(key):
                                raise ValueError(f'Manifest {key} differs; preserve the original fixed target and construction strategy when resuming.')
            if resume and expected.get('constructionStrategy') == FIXED_STRATEGY and not has_identity:
                raise ValueError('Fixed-target resume requires its original manifest to verify target identity before submitting.')
            handle.seek(0,2)
            yield handle
        finally:
            if fcntl is not None: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def log(handle,event,**fields):
    handle.write(json.dumps(dict(event=event,time=datetime.now(timezone.utc).isoformat(),**fields),ensure_ascii=False)+'\n')
    handle.flush()
    os.fsync(handle.fileno())


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server',default='localhost')
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--format',choices=['auto','fasta','jsonl'],default='auto',help='Auto checks extension and first nonempty record for agreement.')
    p.add_argument('--fixed-target',type=Path,help='FASTA with 1–25 fixed chains; each candidate FASTA record becomes its own complete complex.')
    p.add_argument('--engine',choices=['reference','alphafold3','colabfold'])
    p.add_argument('--name')
    p.add_argument('--name-prefix',default='')
    p.add_argument('--database-profile')
    p.add_argument('--execution-profile',type=execution_profile)
    p.add_argument('--placement',choices=['local','aliyun','hybrid'])
    p.add_argument('--seeds',type=parse_seeds)
    p.add_argument('--samples',type=int)
    p.add_argument('--recycles',type=int)
    p.add_argument('--example',help='Reference fixture ID; for small control-plane tests only.')
    p.add_argument('--chunk-size',type=int,default=100)
    p.add_argument('--campaign-id')
    p.add_argument('--manifest',type=Path)
    p.add_argument('--start-streaming',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--limit',type=int)
    p.add_argument('--leave-paused',action='store_true',help='Seal after import but do not resume execution.')
    return p


def settings(args,existing=None):
    old=(existing or {}).get('settings',{})
    engine=args.engine or (existing or {}).get('engine')
    if not engine: raise ValueError('--engine is required for a new campaign or dry run.')
    if existing and args.engine and args.engine!=existing['engine']:
        raise ValueError('--engine conflicts with the existing campaign.')
    result={}
    for attr,key,default in [('seeds','seeds',[1]),('samples','numSamples',1 if engine=='reference' else 5),('recycles','numRecycles',10 if engine=='alphafold3' else 3)]:
        supplied=getattr(args,attr)
        if existing and supplied is not None and supplied!=old.get(key,default):
            raise ValueError(f'--{attr} conflicts with existing campaign settings.')
        result[key]=supplied if supplied is not None else old.get(key,default)
    sample_limit = 5 if engine == 'colabfold' else 20
    if not 1<=result['numSamples']<=sample_limit: raise ValueError(f'--samples must be from 1 to {sample_limit}.')
    if not 1<=result['numRecycles']<=50: raise ValueError('--recycles must be from 1 to 50.')
    if args.example or old.get('example'): result['example']=args.example or old.get('example')
    if existing and args.example and args.example!=old.get('example'):
        raise ValueError('--example conflicts with the existing campaign.')
    validate_item_settings({'settings':result}, engine)
    db=args.database_profile or (existing or {}).get('databaseProfile',{}).get('id') or ('reference' if engine=='reference' else None)
    if not db: raise ValueError('--database-profile is required for a real staged engine.')
    if existing and args.database_profile and args.database_profile!=existing['databaseProfile']['id']:
        raise ValueError('--database-profile conflicts with existing campaign.')
    execution=args.execution_profile if args.execution_profile is not None else (existing or {}).get('executionProfile',{})
    if existing and args.execution_profile is not None and execution!=existing.get('executionProfile',{}):
        raise ValueError('--execution-profile conflicts with existing campaign.')
    placement=getattr(args,'placement',None) or (existing or {}).get('placement','hybrid')
    if existing and getattr(args,'placement',None) and placement!=existing.get('placement','hybrid'):
        raise ValueError('--placement conflicts with the existing campaign.')
    return dict(name=args.name or args.input.stem,engine=engine,settings=result,databaseProfile=db,executionProfile=execution,status='paused',placement=placement)


def build_item(ordinal,record,prefix):
    return dict(targetId=target_id(ordinal,record.name),name=(prefix+record.name)[:120],
                chains=[dict(id='A',sequence=record.sequence)])


def run(args):
    args.server=normalize_server(args.server)
    if not 1<=args.chunk_size<=100: raise ValueError('--chunk-size must be from 1 to 100.')
    if args.limit is not None and args.limit<1: raise ValueError('--limit must be positive.')
    if args.start_streaming and args.leave_paused: raise ValueError('--start-streaming and --leave-paused cannot be combined.')
    if args.campaign_id and not re.fullmatch('[0-9a-f]{32}',args.campaign_id): raise ValueError('Invalid campaign ID.')
    maximum = residue_limit()
    input_format = detect_format(args.input, getattr(args,'format','auto'), maximum)
    fixed_path = getattr(args,'fixed_target',None)
    if fixed_path and input_format != 'fasta':
        raise ValueError('--fixed-target is supported only with candidate FASTA, not JSONL.')
    fixed = load_fixed_target(fixed_path, maximum) if fixed_path else []
    strategy = FIXED_STRATEGY if fixed else INDEPENDENT_STRATEGY
    fixed_sha = file_sha(fixed_path) if fixed_path else None
    fixed_chains_sha = canonical_chains_sha(fixed) if fixed else None
    input_identity = dict(inputFormat=input_format,constructionStrategy=strategy,chainIdPolicy=strategy,
                          fixedTargetSha256=fixed_sha,fixedTargetChainsSha256=fixed_chains_sha)
    with args.input.open(encoding='utf-8-sig') as handle:
        targets=iter_targets(handle,input_format,fixed,args.name_prefix,maximum,args.engine)
        if args.limit is not None: targets=itertools.islice(targets,args.limit)
        numbered=enumerate(targets,1)
        first=next(numbered,None)
        if first is None: raise ValueError('Input contains no targets.')
        numbered=itertools.chain([first],numbered)
        if args.dry_run:
            spec=settings(args)
            count,total_chains,total_residues,examples=0,0,0,[]
            for ordinal,item in numbered:
                validate_item_settings(item,spec['engine'])
                count+=1
                length=sum(len(c['sequence']) for c in item['chains'])
                total_chains+=len(item['chains']);total_residues+=length
                if len(examples)<2:
                    examples.append(dict(targetId=item['targetId'],recordName=item['name'],sequenceLength=length,chainCount=len(item['chains'])))
            return dict(dryRun=True,targets=count,chunks=(count+args.chunk_size-1)//args.chunk_size,
                        combinations=count,fixedTargetChains=len(fixed),fixedTargetResidues=sum(len(c['sequence']) for c in fixed),
                        totalChains=total_chains,totalResidues=total_residues,maxResiduesPerTarget=maximum,
                        campaignId=args.campaign_id,specification=spec,examples=examples,networkAccess=False,manifestWritten=False,
                        **input_identity)
        existing=request_json(args.server,'/api/campaigns/'+args.campaign_id) if args.campaign_id else None
        spec=settings(args,existing)
        if existing and existing.get('status')=='cancelled': raise ValueError('Cancelled campaigns cannot be resumed.')
        manifest=args.manifest or Path(str(args.input)+'.campaign.jsonl')
        identity=dict(server=args.server,inputSha256=file_sha(args.input),campaignId=args.campaign_id,namePrefix=args.name_prefix,**input_identity)
        with manifest_writer(manifest,bool(args.campaign_id),identity) as journal:
            campaign=existing or request_json(args.server,'/api/campaigns',spec)
            campaign_id=campaign.get('id')
            if not isinstance(campaign_id,str) or not re.fullmatch('[0-9a-f]{32}',campaign_id):
                raise RuntimeError('Unexpected campaign acknowledgement; inspect the campaign list.')
            identity['campaignId']=campaign_id
            log(journal,'attached' if existing else 'created',**identity,specification=spec)
            base='/api/campaigns/'+campaign_id
            request_json(args.server,base+'/pause',{})
            accepted=inserted=duplicates=chunks=0
            pending=[]
            started=False
            def flush():
                nonlocal accepted,inserted,duplicates,chunks,started
                response=request_json(args.server,base+'/items',{'items':[x[0] for x in pending]})
                returned=response.get('items',[])
                if (response.get('count')!=len(pending) or len(returned)!=len(pending)
                    or [x.get('targetId') for x in returned]!=[x[0]['targetId'] for x in pending]):
                    raise RuntimeError('Unexpected item acknowledgement. Replay the same file with --campaign-id.')
                new=response.get('inserted')
                duplicate=response.get('duplicates')
                if not isinstance(new,int) or not isinstance(duplicate,int) or new+duplicate!=len(pending):
                    raise RuntimeError('Invalid inserted/duplicate counts in acknowledgement.')
                log(journal,'chunk-accepted',campaignId=campaign_id,chunk=chunks+1,inserted=new,duplicates=duplicate,
                    firstOrdinal=pending[0][1],lastOrdinal=pending[-1][1],
                    items=[dict(targetId=item['targetId'],itemId=ack['id'],chainsSha256=canonical_chains_sha(item['chains'])) for (item,_),ack in zip(pending,returned)])
                accepted+=len(pending); inserted+=new; duplicates+=duplicate; chunks+=1
                pending.clear()
                if args.start_streaming and not started:
                    request_json(args.server,base+'/resume',{})
                    started=True
            try:
                for ordinal,item in numbered:
                    validate_item_settings(item,spec['engine'])
                    pending.append((item,ordinal))
                    if len(pending)==args.chunk_size: flush()
                if pending: flush()
                request_json(args.server,base+'/seal',{})
                if not args.leave_paused: request_json(args.server,base+'/resume',{})
                log(journal,'import-complete',campaignId=campaign_id,accepted=accepted,inserted=inserted,duplicates=duplicates,sealed=True,running=not args.leave_paused)
            except BaseException as exc:
                pause_error=None
                try: request_json(args.server,base+'/pause',{})
                except Exception as pause_exc: pause_error=str(pause_exc)
                log(journal,'import-interrupted',campaignId=campaign_id,error=str(exc),pauseError=pause_error)
                print(f'Campaign {campaign_id}; rerun the same input with --campaign-id {campaign_id}. '+('Pause was not confirmed.' if pause_error else 'Campaign paused.'),file=sys.stderr)
                raise
        return dict(campaignId=campaign_id,acknowledged=accepted,inserted=inserted,duplicates=duplicates,chunks=chunks,sealed=True,running=not args.leave_paused,manifest=str(manifest),fixedTargetChains=len(fixed),**input_identity)


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=run(args)
    except (ValueError,OSError,RuntimeError) as exc:
        print(f'Error: {exc}',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
