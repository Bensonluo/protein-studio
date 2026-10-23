#!/usr/bin/env python3
"""Explicit controller preparation-cache management; no implicit deletion.

Examples:
  python scripts/manage_cache.py --data-dir /srv/foldspace/data scan
  python scripts/manage_cache.py --data-dir /srv/foldspace/data plan --action quarantine
  python scripts/manage_cache.py --data-dir /srv/foldspace/data apply --plan-id ID --digest SHA
The last command is a dry run unless --apply is supplied. Permanent deletion
additionally requires --purge and a previously previewed purge plan.
"""
from pathlib import Path
import argparse
import json
import signal
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from backend.cache_lifecycle import CacheLifecycle


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=ROOT/'data')
    commands=p.add_subparsers(dest='command')
    commands.add_parser('status')
    scan=commands.add_parser('scan')
    scan.add_argument('--max-entries',type=int,default=100000)
    scan.add_argument('--max-seconds',type=float,default=30)
    entries=commands.add_parser('entries')
    entries.add_argument('--scan-id',required=True)
    entries.add_argument('--kind',choices=['whole','leaf','partial','quarantine','unknown'])
    entries.add_argument('--limit',type=int,default=50);entries.add_argument('--offset',type=int,default=0)
    plan=commands.add_parser('plan')
    plan.add_argument('--action',choices=['quarantine','restore','purge'],default='quarantine')
    plan.add_argument('--scan-id')
    plan.add_argument('--kind',action='append',dest='kinds',choices=['whole','leaf','partial','quarantine','unknown'])
    plan.add_argument('--quarantine-id')
    plan.add_argument('--min-age-seconds',type=int,default=7*86400)
    plan.add_argument('--retention-seconds',type=int,default=7*86400)
    detail=commands.add_parser('show-plan')
    detail.add_argument('--plan-id',required=True)
    detail.add_argument('--limit',type=int,default=50);detail.add_argument('--offset',type=int,default=0)
    apply=commands.add_parser('apply')
    apply.add_argument('--plan-id',required=True);apply.add_argument('--digest',required=True)
    apply.add_argument('--apply',action='store_true',help='Apply this exact plan; omission prints a dry-run preview.')
    apply.add_argument('--purge',action='store_true',help='Explicitly permit permanent deletion for an approved purge plan.')
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    cancelled=threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,lambda *_:cancelled.set())
    try:
        manager=CacheLifecycle(args.data_dir)
        command=args.command or 'status'
        if command=='status':result=manager.snapshot()
        elif command=='scan':result=manager.scan(args.max_entries,args.max_seconds,cancelled.is_set)
        elif command=='entries':result=manager.entries(args.scan_id,args.limit,args.offset,args.kind)
        elif command=='plan':result=manager.plan(action=args.action,scan_id=args.scan_id,min_age_seconds=args.min_age_seconds,
            retention_seconds=args.retention_seconds,kinds=args.kinds,quarantine_id=args.quarantine_id,cancelled=cancelled.is_set)
        elif command=='show-plan':result=manager.plan_details(args.plan_id,args.limit,args.offset)
        else:
            preview=manager.plan_details(args.plan_id)
            if args.digest!=preview['plan']['digest']:raise ValueError('Supplied digest differs from the concrete plan.')
            if not args.apply:result=dict(preview,dryRun=True,applied=False)
            else:
                if preview['plan']['action']=='purge' and not args.purge:
                    raise ValueError('Permanent deletion requires --purge as well as --apply and the exact plan digest.')
                result=manager.apply(args.plan_id,args.digest,confirm=True,cancelled=cancelled.is_set)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get('status') in {'failed','partial','cancelled'} else 0
    except Exception as exc:
        print(json.dumps({'error':str(exc),'applied':False},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
