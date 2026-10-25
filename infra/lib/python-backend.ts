import { spawnSync } from 'node:child_process';
import { chmodSync, cpSync, existsSync } from 'node:fs';
import path from 'node:path';

import { aws_lambda as lambda } from 'aws-cdk-lib';

// v1.1.0; layer 30 is published in the current README (the tagged README still lists 28).
// https://github.com/awslabs/aws-lambda-web-adapter/blob/main/README.md#zip-packages
export const LAMBDA_ADAPTER_LAYER_VERSION = 30;

export function pythonBackendCode(): lambda.AssetCode {
  const backend = path.join(__dirname, '..', '..', 'backend');
  const venvPython = path.join(backend, '..', '.venv', 'bin', 'python');
  const python = process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : 'python3.12');
  const pipArgs = [
    '-m', 'pip', 'install', '--disable-pip-version-check', '--progress-bar', 'off',
    '--platform', 'manylinux2014_aarch64', '--only-binary=:all:',
    '--python-version', '3.12', '--implementation', 'cp',
    '-r', path.join(backend, 'requirements-aws.lock'),
  ];
  return lambda.Code.fromAsset(backend, {
    exclude: ['tests', '.venv', '**/__pycache__', '**/*.pyc', '**/*.egg-info', '.pytest_cache', '.ruff_cache', 'build'],
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      platform: 'linux/arm64',
      local: {
        tryBundle(outputDir: string): boolean {
          // Bytecode is only valid for the interpreter that wrote it; anything but
          // Python 3.12 falls back to the Lambda 3.12 bundling image.
          const version = spawnSync(python, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'], {
            encoding: 'utf8',
          });
          if (version.status !== 0 || version.stdout.trim() !== '3.12') return false;
          const result = spawnSync(python, [...pipArgs, '--target', outputDir], {
            cwd: backend,
            env: { ...process.env, PIP_CACHE_DIR: path.join(backend, '..', '.cache', 'pip') },
            stdio: 'inherit',
          });
          if (result.status !== 0) throw new Error('Python Lambda dependency installation failed');
          cpSync(path.join(backend, 'src', 'kb_assistant'), path.join(outputDir, 'kb_assistant'), {
            recursive: true,
            filter: (source) => !source.includes('__pycache__') && !source.endsWith('.pyc'),
          });
          cpSync(path.join(backend, 'lambda', 'run.sh'), path.join(outputDir, 'run.sh'));
          chmodSync(path.join(outputDir, 'run.sh'), 0o755);
          const compiled = spawnSync(python, [
            '-m', 'compileall', '-q', '-f', '--invalidation-mode', 'unchecked-hash',
            '-s', outputDir, '-p', '/var/task', outputDir,
          ], { stdio: 'inherit' });
          if (compiled.status !== 0) throw new Error('Python Lambda bytecode compilation failed');
          return true;
        },
      },
      command: ['bash', '-c', [
        'python -m pip install --disable-pip-version-check --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12 --implementation cp --target /asset-output -r /asset-input/requirements-aws.lock',
        'cp -R /asset-input/src/kb_assistant /asset-output/kb_assistant',
        'find /asset-output/kb_assistant -type d -name __pycache__ -prune -exec rm -rf {} +',
        'cp /asset-input/lambda/run.sh /asset-output/run.sh',
        'chmod 0755 /asset-output/run.sh',
        'python -m compileall -q -f --invalidation-mode unchecked-hash -s /asset-output -p /var/task /asset-output',
      ].join(' && ')],
    },
  });
}
