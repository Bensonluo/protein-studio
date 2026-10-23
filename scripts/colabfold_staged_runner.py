#!/usr/bin/env python3
"""Installed local ColabFold search -> A3M -> prediction adapter (stdlib only).

Verified against ColabFold 1.6.3 source. Local preparation uses CPU MMseqs2;
no public MSA service is queried. A prediction input directory can contain several
A3Ms in a manually batched run, but the campaign scheduler submits one target.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

SEARCH_FLAGS = {'threads', 's', 'db1', 'db2', 'db3', 'db4', 'use_env', 'use_env_pairing',
                'use_templates', 'filter', 'expand_eval', 'align_eval', 'diff', 'qsc',
                'max_accept', 'pair_mode', 'pairing_strategy', 'db_load_mode', 'mmseqs',
                'prefilter_mode'}


def build_command(args):
    profile = json.loads(Path(args.profile_json).read_text())
    paths = profile.get('paths', {})
    parameters = dict(profile.get('searchParameters', {}))
    templates = profile.get('templateParameters', {})
    unknown = set(templates) - {'use_templates', 'max_template_date', 'max_template_hits'}
    if unknown:
        raise ValueError('Unsupported ColabFold template parameters: ' + ', '.join(sorted(unknown)))
    template_switch = templates.get('use_templates', parameters.get('use_templates', False))
    if template_switch not in (True, False, 0, 1):
        raise ValueError('use_templates must be a boolean or 0/1.')
    use_templates = bool(template_switch)
    if args.stage == 'prepare':
        command = [args.executable or 'colabfold_search', args.input,
                   profile.get('root') or paths.get('db_root', ''), args.output_dir]
        parameters['use_templates'] = int(use_templates)
        for name, key in [('uniref30_db', 'db1'), ('environment_db', 'db3'), ('template_db', 'db2'), ('mmseqs_binary', 'mmseqs')]:
            if paths.get(name):
                parameters[key] = paths[name]
        for key, value in parameters.items():
            key = key.replace('-', '_')
            if key not in SEARCH_FLAGS:
                raise ValueError(f'Unsupported ColabFold search parameter: {key}')
            flag = '-s' if key == 's' else '--' + (key if key == 'pairing_strategy' else key.replace('_', '-'))
            command.extend([flag, str(int(value)) if isinstance(value, bool) else str(value)])
        command.extend(['--gpu', '0', '--gpu-server', '0', '--unpack', '1'])
    else:
        if not args.job_json or args.seed is None:
            raise ValueError('--job-json and --seed are required for prediction.')
        job = json.loads(Path(args.job_json).read_text())
        command = [args.executable or 'colabfold_batch', args.input, args.output_dir,
                   '--num-models', str(job['numSamples']), '--num-seeds', '1',
                   '--random-seed', str(args.seed), '--num-recycle', str(job['numRecycles'])]
        model_dir = getattr(args, 'model_dir', None) or paths.get('model_dir')
        if model_dir:
            command.extend(['--data', model_dir])
        if use_templates:
            # Templates may still require local CPU featurization during this
            # model process, but all search is local and no web fallback is used.
            root = Path(args.input) if Path(args.input).is_dir() else Path(args.input).parent
            hits = sorted(root.glob('*.m8'))
            if len(hits) != 1 or not paths.get('local_pdb_path'):
                raise ValueError('Local template prediction requires exactly one prepared .m8 and paths.local_pdb_path; no public template fallback is configured.')
            command.extend(['--templates', '--pdb-hit-file', str(hits[0]), '--local-pdb-path', paths['local_pdb_path']])
            for key in ('max_template_date', 'max_template_hits'):
                if key in templates:
                    command.extend(['--' + key.replace('_', '-'), str(templates[key])])
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['prepare', 'predict'], required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--profile-json', required=True)
    parser.add_argument('--job-json')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--executable')
    parser.add_argument('--model-dir', help='Already installed model-parameter directory for colabfold_batch --data.')
    args = parser.parse_args()
    command = build_command(args)
    if args.stage == 'prepare':
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
    os.execvpe(command[0], command, os.environ.copy())


if __name__ == '__main__':
    main()
