#!/usr/bin/env python3
"""Print safely quoted shell exports from an administrator staged profile.

Example: eval "$(python3 scripts/staged_env.py --config config/staged-engines.local.json --profile af3-local-staged)"
This only prints configuration. It does not start jobs or install model runtimes.
"""
import argparse
import json
from pathlib import Path
import re
import shlex


def exports(config, profile):
    values = config['profiles'][profile]['environment']
    result = []
    for key, value in values.items():
        if not re.fullmatch(r'PROTEIN_STUDIO_[A-Z0-9_]+|JAX_COMPILATION_CACHE_DIR', key):
            raise ValueError('Unsupported environment variable in staged profile: ' + key)
        if key.endswith('_COMMAND'):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and '\0' not in v for v in value):
                raise ValueError('Command values must be nonempty argv arrays.')
            value = json.dumps(value)
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('Environment values must be strings or command argv arrays.')
        result.append('export ' + key + '=' + shlex.quote(value))
    return '\n'.join(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    print(exports(json.loads(args.config.read_text()), args.profile))


if __name__ == '__main__':
    main()
