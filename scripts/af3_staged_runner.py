#!/usr/bin/env python3
"""Run installed AF3 v3.0.4-compatible CPU/GPU stages without shell expansion.

Official interface: google-deepmind/alphafold3/v3.0.4/run_alphafold.py.
No models/databases are installed by this helper. --help requires only stdlib.
--input may be a JSON file or, for a manually batched run, a directory of JSONs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

PATH_FLAGS = {
    'uniref90_database_path', 'mgnify_database_path', 'small_bfd_database_path',
    'uniprot_cluster_annot_database_path', 'seqres_database_path', 'pdb_database_path',
    'ntrna_database_path', 'rfam_database_path', 'rna_central_database_path',
    'jackhmmer_binary_path', 'nhmmer_binary_path', 'hmmalign_binary_path',
    'hmmsearch_binary_path', 'hmmbuild_binary_path',
}
PARAM_FLAGS = {
    'jackhmmer_n_cpu', 'nhmmer_n_cpu', 'jackhmmer_max_parallel_shards',
    'nhmmer_max_parallel_shards', 'max_template_date',
    'small_bfd_z_value', 'mgnify_z_value', 'uniprot_cluster_annot_z_value',
    'uniref90_z_value', 'ntrna_z_value', 'rfam_z_value', 'rna_central_z_value',
}
MANAGED = {'json_path', 'input_dir', 'output_dir', 'run_data_pipeline', 'run_inference',
           'num_seeds', 'num_recycles', 'num_diffusion_samples', 'compress_large_output_files'}


def build_command(args, extra):
    profile = json.loads(Path(args.profile_json).read_text())
    source = Path(args.input)
    if not source.exists():
        raise ValueError('Stage input does not exist.')
    for value in extra:
        if value.startswith('--') and value[2:].split('=', 1)[0] in MANAGED:
            raise ValueError('A managed AF3 stage option cannot be overridden.')
    command = [args.python, args.alphafold_script,
               ('--input_dir=' if source.is_dir() else '--json_path=') + str(source),
               '--output_dir=' + args.output_dir,
               '--run_data_pipeline=' + str(args.stage == 'prepare').lower(),
               '--run_inference=' + str(args.stage == 'predict').lower(),
               '--compress_large_output_files=false']
    if args.stage == 'prepare':
        if profile.get('root'):
            command.append('--db_dir=' + profile['root'])
        for key, value in profile.get('paths', {}).items():
            if key in PATH_FLAGS:
                command.append(f'--{key}={value}')
        for key, value in profile.get('executables', {}).items():
            if key not in PATH_FLAGS or not key.endswith('_binary_path'):
                raise ValueError('Unknown AF3 executable setting: '+key)
            command.append(f'--{key}={value}')
        parameters = dict(profile.get('searchParameters', {}))
        parameters.update(profile.get('templateParameters', {}))
        for key, value in parameters.items():
            key = key.replace('-', '_')
            if key not in PARAM_FLAGS:
                raise ValueError(f'Unsupported AF3 preparation parameter {key}; use a version-specific administrator runner.')
            command.append(f'--{key}={str(value).lower() if isinstance(value, bool) else value}')
    else:
        if not args.job_json:
            raise ValueError('--job-json is required for prediction.')
        job = json.loads(Path(args.job_json).read_text())
        command.extend([f'--num_recycles={job["numRecycles"]}', f'--num_diffusion_samples={job["numSamples"]}'])
        if profile.get('paths', {}).get('model_dir'):
            command.append('--model_dir=' + profile['paths']['model_dir'])
        # max_template_date also controls a CCD conformer fallback at inference.
        date = profile.get('templateParameters', {}).get('max_template_date')
        if date:
            command.append('--max_template_date=' + str(date))
    return command + extra


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['prepare', 'predict'], required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--profile-json', required=True)
    parser.add_argument('--job-json')
    parser.add_argument('--alphafold-script', required=True)
    parser.add_argument('--python', default=sys.executable)
    args, extra = parser.parse_known_args()
    if extra and extra[0] == '--':
        extra = extra[1:]
    command = build_command(args, extra)
    if args.stage == 'prepare':
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
        os.environ['JAX_PLATFORMS'] = 'cpu'
    os.execvpe(command[0], command, os.environ.copy())


if __name__ == '__main__':
    main()
