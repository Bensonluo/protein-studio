#!/usr/bin/env python3
"""Run installed AlphaFold 2 with explicit recycles and safe confidence exports.

Validated against the upstream v2.3.2 API shape, not GPU-tested here:
https://github.com/google-deepmind/alphafold/blob/v2.3.2/run_alphafold.py
https://github.com/google-deepmind/alphafold/blob/v2.3.2/alphafold/model/config.py

Execute with the Python interpreter belonging to the existing AF2 installation.
Arguments after `--` are administrator-supplied standard AF2 CLI flags (database
paths, data_dir, max_template_date, relaxation, binary paths). Nothing is installed
or downloaded. Each invocation handles one pipeline seed; AF2 derives individual
model seeds internally. Recycles are an upper bound when multimer early stopping
is active. The original installed source is not edited.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import sys


def configure_recycles(configuration, number, name):
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 50:
        raise ValueError('numRecycles must be an integer from 1 to 50.')
    if 'model' not in configuration or 'num_recycle' not in configuration['model']:
        raise RuntimeError('Unsupported AF2 model config: model.num_recycle is missing.')
    configuration['model']['num_recycle'] = number
    if 'multimer' not in name:
        if 'data' not in configuration or 'common' not in configuration['data'] or 'num_recycle' not in configuration['data']['common']:
            raise RuntimeError('Unsupported AF2 monomer config: data.common.num_recycle is missing.')
        configuration['data']['common']['num_recycle'] = number
    return configuration


def _plain(value):
    if hasattr(value, 'tolist'):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _plain(v) for key, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def safe_confidence(prediction):
    keys = ('plddt', 'predicted_aligned_error', 'ptm', 'iptm', 'ranking_confidence')
    return {('ranking_score' if key == 'ranking_confidence' else key): _plain(prediction[key])
            for key in keys if key in prediction}


def _passthrough_flags(arguments, multimer):
    managed = {'fasta_paths', 'output_dir', 'random_seed', 'model_preset',
               'num_multimer_predictions_per_model', 'benchmark'}
    omitted = {'pdb70_database_path'} if multimer else {'pdb_seqres_database_path', 'uniprot_database_path'}
    output, i = [], 0
    while i < len(arguments):
        arg = arguments[i]
        if not arg.startswith('--'):
            raise ValueError(f'Expected an administrator AF2 flag, got {arg!r}.')
        key = arg[2:].split('=', 1)[0]
        if key in managed or key == 'nobenchmark':
            raise ValueError(f'--{key} is controlled by the submission adapter and may not be overridden.')
        pair = [arg]
        if '=' not in arg and i + 1 < len(arguments) and not arguments[i + 1].startswith('--'):
            pair.append(arguments[i + 1])
            i += 1
        if key not in omitted:
            output.extend(pair)
        i += 1
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--job-json', type=Path, required=True)
    parser.add_argument('--fasta', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--alphafold-script', type=Path, required=True)
    parser.add_argument('af2_flags', nargs=argparse.REMAINDER, help='Standard AF2 CLI flags after --.')
    args = parser.parse_args(argv)
    flags = args.af2_flags[1:] if args.af2_flags[:1] == ['--'] else args.af2_flags
    job = json.loads(args.job_json.read_text())
    chains = job.get('chains')
    if not isinstance(chains, list) or not chains:
        parser.error('job-json must contain normalized protein chains.')
    multimer = len(chains) > 1
    preset = 'multimer' if multimer else 'monomer_ptm'
    samples = job.get('numSamples', 1)
    if not isinstance(samples, int) or isinstance(samples, bool) or not 1 <= samples <= 20:
        parser.error('numSamples must be an integer from 1 to 20.')
    if not multimer and samples != 1:
        parser.error('Monomer requires numSamples=1; use more pipeline seeds for extra runs.')
    if args.seed not in job.get('seeds', []):
        parser.error('seed must be present in the normalized submission.')
    script = args.alphafold_script.resolve()
    if not script.is_file() or not args.fasta.is_file():
        parser.error('Existing alphafold-script and fasta files are required.')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve()
    fasta = args.fasta.resolve()
    forwarded = _passthrough_flags(flags, multimer)
    # The installed repository must take precedence over a similarly named
    # package elsewhere. This happens in a separate adapter process, not server.
    sys.path.insert(0, str(script.parent))
    from alphafold.model import config, model

    original_config = config.model_config
    original_init = model.RunModel.__init__
    original_predict = model.RunModel.predict
    config_names = {}
    recycles = job.get('numRecycles', 3)

    def configured(name):
        result = configure_recycles(original_config(name), recycles, name)
        config_names[id(result)] = name
        return result

    def init(instance, configuration, *rest, **kwargs):
        original_init(instance, configuration, *rest, **kwargs)
        instance._studio_model_name = config_names.get(id(configuration))
        instance._studio_prediction_index = 0

    def predict(instance, *positional, **kwargs):
        result = original_predict(instance, *positional, **kwargs)
        name = instance._studio_model_name
        if name is None:
            raise RuntimeError('AF2 API changed: no model name associated with this runner.')
        destination = output / fasta.stem
        destination.mkdir(parents=True, exist_ok=True)
        index = instance._studio_prediction_index
        # Upstream v2.3.2 predict_structure names both monomer and multimer
        # outputs <model_name>_pred_<i>. Export directly from the live return
        # value, avoiding any unpickling of result files in the web process.
        path = destination / f'result_{name}_pred_{index}.json'
        path.write_text(json.dumps(safe_confidence(result), allow_nan=False))
        instance._studio_prediction_index += 1
        return result

    config.model_config = configured
    model.RunModel.__init__ = init
    model.RunModel.predict = predict
    sys.argv = [str(script), f'--fasta_paths={fasta}', f'--output_dir={output}',
                f'--random_seed={args.seed}', f'--model_preset={preset}', '--benchmark=false',
                f'--num_multimer_predictions_per_model={samples}', *forwarded]
    (output / 'adapter_manifest.json').write_text(json.dumps(dict(
        adapter='protein-studio/scripts/af2_runner.py', upstreamApi='AlphaFold v2.3.2',
        upstreamApiMeaning='API compatibility target, not a claim about the installed release',
        alphafoldScript=str(script), modelPreset=preset, pipelineSeed=args.seed,
        requestedMaxRecycles=recycles, predictionsPerModel=samples if multimer else 1,
        expectedModelWeights=5, argv=sys.argv,
    ), indent=2))
    try:
        runpy.run_path(str(script), run_name='__main__')
    finally:
        config.model_config = original_config
        model.RunModel.__init__ = original_init
        model.RunModel.predict = original_predict


if __name__ == '__main__':
    main()
