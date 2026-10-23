#!/usr/bin/env python3
"""Generate a consistent real AF3 deployment from one explicit host inventory.

No remote connection, model download, cloud provisioning or process launch occurs.
Actual bundles contain secrets; keep them outside source archives.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import tempfile
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
AF3_COMMIT='85c4d20505fd5cef05eac22b534d4e793971ae69'
DEFAULTS={'projectRoot':'/srv/foldspace','python':'/opt/foldspace/bin/python',
          'af3Python':'/opt/af3/venv/bin/python','af3Script':'/opt/af3/run_alphafold.py',
          'modelDir':'/srv/af3/models','workRoot':'/var/lib/foldspace-workers',
          'compilationCache':'/srv/af3/cache'}
GPU_ENV={'XLA_FLAGS':'--xla_gpu_enable_triton_gemm=false',
         'XLA_PYTHON_CLIENT_PREALLOCATE':'true','XLA_CLIENT_MEM_FRACTION':'0.95'}

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def identifier(value,name):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}',value):
        raise ValueError(name+' must be a lowercase host identifier.')
    return value

def path(value,name):
    if not isinstance(value,str) or not value.startswith('/') or any(c in value for c in ('\0','\n','\r')) or '..' in PurePosixPath(value).parts:
        raise ValueError(name+' must be an absolute Linux path without traversal.')
    return str(PurePosixPath(value))

def integer(value,name,minimum=1,maximum=1048576):
    if isinstance(value,bool) or not isinstance(value,int) or not minimum<=value<=maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}.')
    return value

def normalize(raw,preview=False):
    data=copy.deepcopy(raw);unresolved=[]
    if data.get('schemaVersion')!=1:raise ValueError('Inventory requires schemaVersion=1.')
    if data.get('example'):
        unresolved.append('example=true; replace host inventory and set false')
    defaults={**DEFAULTS,**data.get('paths',{})}
    if set(defaults)-set(DEFAULTS):raise ValueError('Unknown shared path setting.')
    defaults={k:path(v,k) for k,v in defaults.items()}
    control=data.get('controller',{})
    identifier(control.get('id'),'controller.id')
    control['paths']={**defaults,**control.get('paths',{})}
    if set(control['paths'])-set(DEFAULTS):raise ValueError('Unknown controller path setting.')
    for k,v in control['paths'].items():path(v,'controller.'+k)
    origin=control.get('workerURL','');parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('controller.workerURL must be an HTTPS origin.')
    if parsed.hostname.endswith(('.example.org','.example.com','.example.net')) or parsed.hostname in {'example.org','example.com','example.net'}:
        unresolved.append('controller.workerURL is an example domain')
    control['workerURL']=origin.rstrip('/')
    control['port']=integer(control.get('port',8765),'controller.port',1024,65535)
    control['databaseRoot']=path(control.get('databaseRoot','/srv/af3/databases'),'databaseRoot')
    control['hmmerBinDir']=path(control.get('hmmerBinDir','/opt/hmmer/bin'),'hmmerBinDir')
    control['dataDir']=path(control.get('dataDir','/var/lib/foldspace'),'dataDir')
    release=control.get('databaseRelease')
    if not isinstance(release,str) or not release or any(x in release.upper() for x in ('REPLACE','SET_','UNSET')):
        unresolved.append('controller.databaseRelease must identify the installed database snapshot')
    for k,default,maximum in [('prepareWorkers',1,32),('searchThreads',8,256),('validationWorkers',2,16),('readyCap',64,10000)]:
        control[k]=integer(control.get(k,default),'controller.'+k,1,maximum)
    model=data.get('model',{})
    if model.get('sourceCommit',AF3_COMMIT)!=AF3_COMMIT:
        raise ValueError('This adapter deployment is pinned to AF3 v3.0.4 commit '+AF3_COMMIT)
    files=model.get('weights',[])
    if not isinstance(files,list) or not files:raise ValueError('model.weights must list every selected weight file and its SHA-256.')
    names=set();validated=[]
    for entry in files:
        name=entry.get('file');digest=entry.get('sha256','')
        if not isinstance(name,str) or not name or name!=PurePosixPath(name).name or name in {'.','..'} or '\\' in name or any(c in name for c in '\0\r\n') or name in names:
            raise ValueError('Weight files must be distinct safe basenames.')
        names.add(name)
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or len(set(digest))==1:
            unresolved.append('model.weights SHA-256 missing or placeholder: '+name)
        validated.append(dict(file=name,sha256=digest))
    validated.sort(key=lambda x:x['file'])
    manifest_sha=hashlib.sha256(canonical(validated).encode()).hexdigest()
    version='af3-v3.0.4-'+AF3_COMMIT[:12]+'-w'+manifest_sha[:12]
    model.update(sourceCommit=AF3_COMMIT,weights=validated,weightsManifestSha256=manifest_sha,runtimeVersion=version)
    model['samples']=integer(model.get('samples',5),'model.samples',1,5)
    model['recycles']=integer(model.get('recycles',10),'model.recycles',1,20)
    if model.get('attention','triton') not in {'triton','xla'}:raise ValueError('attention must be triton or xla.')
    model.setdefault('attention','triton');model.setdefault('maxTemplateDate','2021-09-30')
    import datetime
    datetime.date.fromisoformat(model['maxTemplateDate'])
    model.setdefault('buckets',[256,512,768,1024,1280,1536,2048,2560,3072,3584,4096,4608,5120])
    if not isinstance(model['buckets'],list) or not model['buckets'] or any(type(v) is not int or v<1 for v in model['buckets']) or sorted(set(model['buckets']))!=model['buckets']:
        raise ValueError('buckets must be strictly increasing positive integers.')
    hosts=data.get('hosts');ids={control['id']};local_count=0
    if not isinstance(hosts,list) or not hosts:raise ValueError('Explicit GPU hosts are required.')
    for host in hosts:
        hid=identifier(host.get('id'),'host.id')
        if hid in ids:raise ValueError('Host IDs must be unique across controller and GPU hosts.')
        ids.add(hid)
        if host.get('location') not in {'local','aliyun'}:raise ValueError('GPU host location must be local or aliyun.')
        devices=host.get('gpuDevices')
        if not isinstance(devices,list) or not devices or len(devices)>64 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+',x) for x in devices) or len(set(devices))!=len(devices):
            raise ValueError('gpuDevices must explicitly enumerate distinct device indices or GPU UUIDs on each host.')
        local_count+=len(devices) if host['location']=='local' else 0
        host['paths']={**defaults,**host.get('paths',{})}
        if set(host['paths'])-set(DEFAULTS):raise ValueError('Unknown GPU host path setting.')
        host['paths']={k:path(v,hid+'.'+k) for k,v in host['paths'].items()}
        host['maxResidues']=integer(host.get('maxResidues',1024),hid+'.maxResidues',1,model['buckets'][-1])
        host['environment']={**GPU_ENV,**host.get('environment',{})}
        for key,value in host['environment'].items():
            if key not in {'XLA_FLAGS','XLA_PYTHON_CLIENT_PREALLOCATE','XLA_CLIENT_MEM_FRACTION','JAX_COMPILATION_CACHE_DIR','JAX_LOG_COMPILES','OMP_NUM_THREADS','TF_CPP_MIN_LOG_LEVEL','SSL_CERT_FILE'} or not isinstance(value,str) or any(c in value for c in '\0\r\n'):
                raise ValueError('Unsupported GPU environment entry: '+key)
    expected=integer(data.get('localGpuCards',16),'localGpuCards',1,1024)
    if local_count!=expected:raise ValueError(f'Explicit local GPU mapping has {local_count} cards; localGpuCards declares {expected}.')
    if not preview and unresolved:raise ValueError('; '.join(unresolved))
    return dict(schemaVersion=1,controller=control,model=model,hosts=hosts,localGpuCards=expected,unresolved=unresolved)

def generate(inventory):
    """Return public files plus private env maps. Never print private values."""
    control,model=inventory['controller'],inventory['model'];version=model['runtimeVersion']
    files={};private={};registry=[];controller_tokens={};worker_ids=[]
    paths=control['paths'];project=paths['projectRoot']
    profile=copy.deepcopy(json.loads((ROOT/'config/databases.example.json').read_text())['profiles']['af3-protein-local'])
    profile.pop('example',None)
    profile.update(validationProfile='af3-v3.0.4',root=control['databaseRoot'],release=control['databaseRelease'],
        prepareVersion='af3-v3.0.4-'+AF3_COMMIT,searchVersion='hmmer-3.4-seq-limit-patch',
        executables={key+'_binary_path':control['hmmerBinDir']+'/'+key for key in ['jackhmmer','nhmmer','hmmalign','hmmsearch','hmmbuild']},
        searchParameters={'jackhmmer_n_cpu':control['searchThreads'],'nhmmer_n_cpu':control['searchThreads']},
        templateParameters={'max_template_date':model['maxTemplateDate']})
    files['controller/databases.json']={'schemaVersion':1,'profiles':{'af3-production':profile}}
    prepare=[paths['af3Python'],project+'/scripts/af3_staged_runner.py','--stage','prepare','--input','{input_json}',
        '--output-dir','{output_dir}','--profile-json','{profile_json}','--job-json','{job_json}',
        '--alphafold-script',paths['af3Script'],'--python',paths['af3Python']]
    environment={'PROTEIN_STUDIO_HOST':'127.0.0.1','PROTEIN_STUDIO_PORT':str(control['port']),
        'PROTEIN_STUDIO_DATA_DIR':control['dataDir'],'PROTEIN_STUDIO_DISTRIBUTED':'1','PROTEIN_STUDIO_PREDICTION_MODE':'workers',
        'PROTEIN_STUDIO_WORKER_CONFIG':'/etc/foldspace/workers.registry.json','PROTEIN_STUDIO_DATABASE_CONFIG':'/etc/foldspace/databases.json',
        'PROTEIN_STUDIO_CPU_PREP_WORKERS':str(control['prepareWorkers']),
        'PROTEIN_STUDIO_CPU_VALIDATE_WORKERS':str(control['validationWorkers']),
        'PROTEIN_STUDIO_READY_HIGH_WATER':str(control['readyCap']),
        'PROTEIN_STUDIO_PREPARE_TIMEOUT_SECONDS':'86400','PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND':prepare,
        'PROTEIN_STUDIO_ALPHAFOLD3_VERSION':version}
    files['controller/controller.json']={'schemaVersion':1,'role':'controller','projectRoot':project,'python':paths['python'],'environment':environment}
    network=urlsplit(control['workerURL'])
    nginx=(ROOT/'deploy/af3/controller.nginx.conf.example').read_text()
    nginx=nginx.replace('fold-worker.example.org',network.hostname).replace('listen 4443 ssl;',f'listen {network.port or 443} ssl;')
    nginx=nginx.replace('127.0.0.1:8765',f"127.0.0.1:{control['port']}")
    files['controller/nginx.conf']=nginx
    for host in inventory['hosts']:
        hp=host['paths'];host_tokens={};fleet=[]
        for ordinal,device in enumerate(host['gpuDevices']):
            wid=f"{host['id']}-gpu-{ordinal:02}";token_env='FOLDSPACE_TOKEN_'+wid.replace('-','_').upper()
            token=secrets.token_urlsafe(36);controller_tokens[token_env]=token;host_tokens[token_env]=token;worker_ids.append(wid)
            work=hp['workRoot']+'/'+wid
            resident=[hp['af3Python'],hp['projectRoot']+'/scripts/resident_runner.py','--engine','alphafold3',
                '--af3-script',hp['af3Script'],'--model-dir',hp['modelDir'],'--model-version',version,
                '--num-samples',str(model['samples']),'--num-recycles',str(model['recycles']),
                '--attention',model['attention'],'--device','0','--buckets',','.join(map(str,model['buckets'])),
                '--max-template-date',model['maxTemplateDate'],'--compilation-cache',hp['compilationCache']+'/'+version,'--allowed-root',work]
            worker={'schemaVersion':1,'role':'gpu-worker','projectRoot':hp['projectRoot'],'python':hp['python'],
                'url':control['workerURL'],'workerId':wid,'tokenEnv':token_env,'engine':'alphafold3',
                'runtimeVersion':version,'workDir':work,'gpuDevices':[device],'samples':model['samples'],'recycles':model['recycles'],
                'residentCommand':resident,'environment':host['environment'],
                'residentStartupTimeout':1800,'residentRequestTimeout':86400,'httpTimeout':600}
            files[f"hosts/{host['id']}/workers/{wid}.json"]=worker
            private[f"hosts/{host['id']}/workers/{wid}.env"]={token_env:token}
            fleet.append({k:worker[k] for k in ['engine','runtimeVersion','workDir','gpuDevices','samples','recycles','residentCommand',
                                             'residentStartupTimeout','residentRequestTimeout','httpTimeout']})
            fleet[-1].update(id=wid,tokenEnv=token_env)
            registry.append({'id':wid,'name':wid,'location':host['location'],'mode':'real','tokenEnv':token_env,
                'engines':['alphafold3'],'maxResidues':host['maxResidues']})
        files[f"hosts/{host['id']}/fleet.json"]={'schemaVersion':1,'controllerURL':control['workerURL'],'workers':fleet}
        private[f"hosts/{host['id']}/fleet.env"]={**host['environment'],**host_tokens}
    private['controller/controller.env']=controller_tokens
    files['controller/workers.registry.json']={'schemaVersion':1,'leaseSeconds':180,'batchTargets':2,'batchResidues':4096,
        'maxBundleBytes':536870912,'maxExpandedBytes':2147483648,'planned':{'localGpuCards':inventory['localGpuCards']},'workers':registry}
    files['weights.expected.json']={'schemaVersion':1,'files':model['weights'],'manifestSha256':model['weightsManifestSha256']}
    files['acceptance.json']={'schemaVersion':1,'server':f"http://127.0.0.1:{control['port']}",'engine':'alphafold3',
        'databaseProfile':'af3-production','executionProfile':{'id':'af3-native','version':version},
        'samples':model['samples'],'recycles':model['recycles'],'seeds':[1],
        'expectedWorkers':worker_ids,'expectedLocalGpuCards':inventory['localGpuCards'],
        'expectedWeightsManifestSha256':model['weightsManifestSha256']}
    files['deployment.json']={'schemaVersion':1,'runtimeVersion':version,'sourceCommit':AF3_COMMIT,
        'controllerId':control['id'],'localGpuCards':inventory['localGpuCards'],'workerCount':len(registry),
        'hosts':[{'id':h['id'],'location':h['location'],'gpuDevices':h['gpuDevices'],'paths':h['paths']} for h in inventory['hosts']],
        'note':'Generated configuration, not evidence of online hardware. Secrets are in private env files. No cloud instances were provisioned.'}
    return files,private

def write_bundle(output,files,private):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Output already exists; preserve its secrets. Use a new directory for an intentional credential rotation.')
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.af3-deploy-',dir=output.parent))
    try:
        os.chmod(temporary,0o700)
        for name,value in files.items():
            target=temporary/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+'\n');os.chmod(target,0o640)
        import shlex
        for name,values in private.items():
            target=temporary/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n');os.chmod(target,0o600)
        os.rename(temporary,output)
    except BaseException:
        shutil.rmtree(temporary,ignore_errors=True);raise

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True);parser.add_argument('--output',type=Path)
    parser.add_argument('--dry-run',action='store_true');args=parser.parse_args(argv)
    data=normalize(json.loads(args.inventory.read_text()),preview=args.dry_run)
    if args.dry_run:
        print(json.dumps({'configurationOnly':True,'localGpuCards':data['localGpuCards'],
            'cloudGpuCards':sum(len(h['gpuDevices']) for h in data['hosts'] if h['location']=='aliyun'),
            'runtimeVersion':data['model']['runtimeVersion'] if not data['unresolved'] else None,
            'unresolved':data['unresolved'],'secretsGenerated':False,'processesStarted':False},ensure_ascii=False,indent=2));return 0
    if not args.output:raise ValueError('--output is required to create a private deployment bundle.')
    files,private=generate(data);write_bundle(args.output,files,private)
    print(json.dumps({'output':str(args.output.resolve()),'workers':len(files['controller/workers.registry.json']['workers']),
        'runtimeVersion':data['model']['runtimeVersion'],'privateEnvFiles':len(private),
        'processesStarted':False,'secretsPrinted':False},ensure_ascii=False));return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,KeyError,OSError) as error:raise SystemExit(str(error))
