"""Collect pinned local models and create ZIP64 archives; never run inference."""
import argparse
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODEL_NOTICES = ("sensevoice-small-int8/LICENSE", "sensevoice-small-int8/README.md", "fa-zh/README.md")
# Reviewed delivery-only exclusions, relative to the portable staging root. PyTorch
# development headers are collected by Nuitka's standard configuration but no
# shipped path compiles C++ extensions (JIT is disabled), and jieba's Paddle model
# is only reached through its unexposed Paddle mode. Keep this list exact: it is
# not a general "delete unused files" pass.
PORTABLE_EXCLUSIONS = ("_runtime/torch/include", "_runtime/jieba/lac_small")
FUNASR_REGISTRY_SOURCE = """\
            class_file = inspect.getfile(target_class)
            class_line = inspect.getsourcelines(target_class)[1]
"""
FUNASR_REGISTRY_STANDALONE = """\
            try:
                class_file = inspect.getfile(target_class)
                class_line = inspect.getsourcelines(target_class)[1]
            except (OSError, TypeError):
                return target_class
"""


def model_payload(source, destination=None):
    manifest = json.loads((ROOT / 'release-assets/offline-models.json').read_text(encoding='utf-8'))
    for relative, expected in manifest.items():
        path = source / relative
        if not path.is_file():
            raise FileNotFoundError(f'Missing local model: {path}. See docs/offline-models.md; no auto-download.')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError(f'Model checksum mismatch: {path}')
    for relative in MODEL_NOTICES:
        if not (source / relative).is_file():
            raise FileNotFoundError(f'Missing model notice: {source / relative}')
    if destination:
        for relative in manifest:
            target = destination / '_runtime/models' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        notices = destination / 'docs/licenses/models'
        for relative in MODEL_NOTICES:
            target = notices / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        shutil.copytree(ROOT / 'release-assets/model-licenses', notices, dirs_exist_ok=True)
        (destination / 'docs/MODEL-CHECKSUMS.txt').write_text(
            ''.join(f'{digest}  _runtime/models/{name}\n' for name, digest in manifest.items()), encoding='utf-8')
    print(f'Local model payload: {len(manifest)} files verified' + (' and collected' if destination else ''))


def prune_portable(staging):
    """Remove the reviewed delivery-only subtrees from a staged portable tree.

    Applies only to a newly created staging directory. Missing subtrees are an
    error: the packaged inventory changed, so the build must be reviewed instead
    of silently shipping the extra bytes.
    """
    staging = Path(staging).resolve()
    if not staging.is_dir():
        raise FileNotFoundError(f'Portable staging directory is missing: {staging}')
    removed = {}
    for relative in PORTABLE_EXCLUSIONS:
        target = staging / relative
        if not target.is_dir():
            raise FileNotFoundError(
                f'Reviewed exclusion is missing from the staged tree: {relative}. '
                'The packaged inventory changed; review the delivery before excluding.'
            )
        files = [path for path in target.rglob('*') if path.is_file()]
        removed[relative] = (len(files), sum(path.stat().st_size for path in files))
        shutil.rmtree(target)
    total_files = sum(count for count, _ in removed.values())
    total_bytes = sum(size for _, size in removed.values())
    for relative, (count, size) in removed.items():
        print(f'Excluded {relative}: {count} files, {size:,} bytes')
    print(f'Portable exclusions: removed {total_files} files, {total_bytes:,} bytes')


def archive(source, destination, include_root=False, compresslevel=9):
    source, destination = source.resolve(), destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError('Archive destination must be outside its input directory')
    temporary = destination.with_suffix('.zip.partial')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=compresslevel, allowZip64=True) as output:
            for path in sorted(source.rglob('*')):
                relative = path.relative_to(source)
                if '__pycache__' in relative.parts or path.suffix in ('.pyc', '.pyo', '.log', '.tmp') or path.name == 'asr_cache.json':
                    continue
                if path.is_file():
                    name = Path(source.name) / relative if include_root else relative
                    output.write(path, name.as_posix())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Archive ready: {destination} ({destination.stat().st_size:,} bytes, '
          f'deflate level {compresslevel})')


def local_lock(source, destination):
    """Keep locked hashes, but prefer manually supplied archives over registry URLs."""
    text = source.read_text(encoding='utf-8')
    def local_requirement(match):
        name, version = match.group(1), match.group(2)
        stem = name.replace('-', '_')
        candidates = list((ROOT / 'build/offline-wheels').glob(f'{stem}-{version}-*.whl'))
        candidates += list((ROOT / 'build/offline-wheels').glob(f'{name}-{version}.tar.gz'))
        if len(candidates) > 1:
            raise ValueError(f'Ambiguous manual package inputs for {name}=={version}')
        return f'{name} @ {candidates[0].resolve().as_uri()}' if candidates else match.group(0)
    text = re.sub(r'^([A-Za-z0-9_.-]+)==([^\s]+)', local_requirement, text, flags=re.MULTILINE)
    destination.write_text(text, encoding='utf-8')


def patch_funasr_registry(register_path=None):
    """Keep registration working when Nuitka omits inspectable Python sources."""
    if register_path is None:
        distribution = importlib.metadata.distribution('funasr')
        register_path = Path(distribution.locate_file('funasr/register.py'))
    source = register_path.read_text(encoding='utf-8')
    if FUNASR_REGISTRY_STANDALONE in source:
        return
    if source.count(FUNASR_REGISTRY_SOURCE) != 1:
        raise RuntimeError(f'Unsupported FunASR registry source: {register_path}')
    patched = source.replace(FUNASR_REGISTRY_SOURCE, FUNASR_REGISTRY_STANDALONE)
    temporary = register_path.with_name(register_path.name + '.asrtools-patch')
    temporary.write_text(patched, encoding='utf-8')
    temporary.replace(register_path)
    print(f'Patched FunASR registry for Nuitka standalone: {register_path}')


def normalize_nuitka_argument(argument):
    """Give generated-profile arguments a stable identity for fingerprinting.

    The generated configuration and module list live under a timestamped build
    directory, so hashing their absolute paths would rebuild on every run and
    would still miss a changed file at a fixed path. Their content is hashed
    separately; the argument keeps only a logical identity.
    """
    for prefix in ('--user-package-configuration-file=',):
        if str(argument).startswith(prefix):
            return prefix + '@inference-profile'
    return str(argument)


def profile_fingerprint_inputs(profile_dir):
    """Content hashes for the authored profile, generator and generated outputs."""
    import hashlib as _hashlib

    directory = Path(profile_dir)
    if not directory.is_dir():
        raise RuntimeError(f'Generated inference profile directory is missing: {directory}')

    def sha256(path):
        digest = _hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    entries = {}
    for relative in ('release-assets/funasr-inference-profile.json',
                     'scripts/prepare_funasr_profile.py'):
        path = ROOT / relative
        if not path.is_file():
            raise RuntimeError(f'Missing inference profile input: {path}')
        entries[relative] = sha256(path)
    for path in sorted(directory.rglob('*')):
        if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_file():
            entries[f'generated/{path.relative_to(directory).as_posix()}'] = sha256(path)
    entries['profile_content_hash'] = _hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode('utf-8')).hexdigest()
    return entries


def check_inference_report(report_path, profile_path):
    """Gate the compiled module inventory against the reviewed inference profile."""
    import xml.etree.ElementTree as ElementTree

    profile = json.loads(Path(profile_path).read_text(encoding='utf-8'))
    report_path = Path(report_path)
    if not report_path.is_file():
        raise RuntimeError(f'Compilation report is missing: {report_path}')
    root = ElementTree.parse(report_path).getroot()
    if root.tag != 'nuitka-compilation-report':
        raise RuntimeError(f'Unexpected compilation report root: {root.tag}')
    if root.get('completion') != 'yes':
        raise RuntimeError(
            f"Compilation report did not complete successfully: {root.get('completion')!r}")
    included = {node.get('name') for node in root.findall('module') if node.get('name')}
    accepted = set(profile['module_closure'])
    funasr_modules = {name for name in included
                      if name == 'funasr' or name.startswith('funasr.')}
    unexpected = sorted(funasr_modules - accepted)
    if unexpected:
        raise RuntimeError(
            f'Compiled FunASR modules outside the reviewed closure: {unexpected}. '
            'Inspect their importer before editing the profile.')
    missing = sorted(accepted - funasr_modules)
    if missing:
        raise RuntimeError(f'Reviewed FunASR modules are absent from the binary: {missing}')
    top_level = {name.split('.')[0] for name in included}
    forbidden = sorted(set(profile['excluded_dependency_candidates']) & top_level)
    if forbidden:
        raise RuntimeError(
            f'Excluded dependency candidates are still compiled in: {forbidden}. '
            'Resolve the remaining importer instead of weakening the gate.')
    conditional = sorted(set(profile.get('conditional_dependency_candidates', [])) & top_level)
    print(f'Inference profile report: {len(funasr_modules)} funasr modules compiled, '
          f'0 unexpected, primary exclusions absent')
    if conditional:
        print(f'  conditional dependencies still present (allowed, measured): {conditional}')
    return {'funasr_modules': len(funasr_modules), 'unexpected': unexpected,
            'conditional_present': conditional}


def compile_fingerprint(args_file, output, profile_dir=None):
    """Fingerprint every input Nuitka standalone compiles, for change-gated rebuilds.

    Hashing repo sources, Nuitka arguments, Python/Nuitka versions, installed
    package versions, and the venv's Python sources lets a later build skip
    compilation entirely when none of the frozen executable's inputs changed.
    """
    import hashlib
    import json
    import sysconfig

    parameters = json.loads(Path(args_file).read_text(encoding='utf-8'))
    # Per-run values do not change the produced binary and would break comparisons.
    stable_args = [
        normalize_nuitka_argument(argument) for argument in parameters.get('args', [])
        if not str(argument).startswith(('--output-dir=', '--report=', '--jobs='))
    ]

    def sha256_file(path):
        hasher = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        return hasher.hexdigest()

    def directory_files(root):
        root = Path(root)
        entries = {}
        if root.is_dir():
            for path in sorted(root.rglob('*')):
                if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                    continue
                if path.is_file():
                    entries[str(path.relative_to(root)).replace('\\', '/')] = sha256_file(path)
        return entries

    repo_files = {}
    for relative in ('asr_gui.py', 'app_runtime.py', '.python-version',
                     'requirements-release.in', 'requirements-release.lock',
                     'bk_asr', 'resources'):
        path = ROOT / relative
        if not path.exists():
            raise RuntimeError(f'Missing compile input for fingerprinting: {path}')
        if path.is_dir():
            for name, digest in directory_files(path).items():
                repo_files[f'{relative}/{name}'] = digest
        else:
            repo_files[relative] = sha256_file(path)

    site_packages = Path(sysconfig.get_paths()['purelib'])
    site_py = {}
    if site_packages.is_dir():
        for path in sorted(site_packages.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            site_py[str(path.relative_to(site_packages)).replace('\\', '/')] = sha256_file(path)

    components = {
        'app_version': parameters.get('appVersion'),
        'python_prefix': sys.prefix,
        'python_version': sys.version,
        'nuitka_version': importlib.metadata.version('nuitka'),
        'distributions': sorted(
            f'{dist.metadata["Name"]}=={dist.version}' for dist in importlib.metadata.distributions()),
        'nuitka_args': stable_args,
        'repo_files': repo_files,
        'site_packages_py': site_py,
    }
    if profile_dir:
        components['inference_profile'] = profile_fingerprint_inputs(profile_dir)
    fingerprint = hashlib.sha256(
        json.dumps(components, sort_keys=True).encode('utf-8')).hexdigest()
    Path(output).write_text(
        json.dumps({**components, 'fingerprint': fingerprint}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    print(f'Compile fingerprint: {fingerprint} '
          f'({len(repo_files)} repo files, {len(site_py)} venv sources)')


def check_environment():
    expected_python = (ROOT / '.python-version').read_text().strip()
    if '.'.join(map(str, sys.version_info[:3])) != expected_python:
        raise RuntimeError(f'Build requires Python {expected_python}')
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Use a project virtual environment for release builds')
    pins = re.findall(r'^([A-Za-z0-9_.-]+)==([^\s]+)', (ROOT / 'requirements-release.lock').read_text(), re.MULTILINE)
    for name, expected in pins:
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError(f'Build dependency mismatch: {name}: expected {expected}, installed {actual}')
    patch_funasr_registry()
    print(f'Build environment: Python {expected_python}, {len(pins)} locked package versions matched')


def prepare_dependency_walker(destination, source):
    """Reuse a complete local tool installation; never prompt or download."""
    required = ('depends.exe', 'depends.dll')
    if not all((destination / name).is_file() for name in required):
        if not all((source / name).is_file() for name in required):
            raise FileNotFoundError(
                f'Dependency Walker is missing. Place depends.exe and depends.dll in '
                f'{destination} before building (existing cache checked: {source}). '
                'No download attempted; compilation has not started.'
            )
        destination.mkdir(parents=True, exist_ok=True)
        for name in required:
            shutil.copy2(source / name, destination / name)


def check_build_tools():
    from nuitka.utils.AppDirs import getAppdirsModule, getCacheDir
    from nuitka.utils.Utils import getArchitecture
    from nuitka.freezer.DllDependenciesWin32DependsExe import getDependsExePath

    relative = Path('depends') / getArchitecture()
    destination = Path(getCacheDir('downloads')) / relative
    source = Path(getAppdirsModule().user_cache_dir('Nuitka', None)) / 'downloads' / relative
    prepare_dependency_walker(destination, source)
    # Use the same resolver as standalone compilation, after the no-download check.
    print(f'Build tools: Dependency Walker ready: {getDependsExePath()}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    models = sub.add_parser('models')
    models.add_argument('--source', type=Path, required=True)
    models.add_argument('--destination', type=Path)
    zip_parser = sub.add_parser('zip')
    zip_parser.add_argument('--source', type=Path, required=True)
    zip_parser.add_argument('--destination', type=Path, required=True)
    zip_parser.add_argument('--include-root', action='store_true')
    zip_parser.add_argument('--compresslevel', type=int, choices=range(0, 10), default=9)
    prune_parser = sub.add_parser('prune')
    prune_parser.add_argument('--source', type=Path, required=True)
    lock_parser = sub.add_parser('local-lock')
    lock_parser.add_argument('--source', type=Path, required=True)
    lock_parser.add_argument('--destination', type=Path, required=True)
    fingerprint_parser = sub.add_parser('compile-fingerprint')
    fingerprint_parser.add_argument('--args-file', type=Path, required=True)
    fingerprint_parser.add_argument('--out', type=Path, required=True)
    fingerprint_parser.add_argument('--profile-dir', type=Path)
    report_parser = sub.add_parser('check-inference-report')
    report_parser.add_argument('--report', type=Path, required=True)
    report_parser.add_argument('--profile', type=Path, required=True)
    sub.add_parser('environment')
    sub.add_parser('build-tools')
    args = parser.parse_args()
    if args.command == 'models':
        model_payload(args.source, args.destination)
    elif args.command == 'zip':
        archive(args.source, args.destination, args.include_root, args.compresslevel)
    elif args.command == 'prune':
        prune_portable(args.source)
    elif args.command == 'local-lock':
        local_lock(args.source, args.destination)
    elif args.command == 'compile-fingerprint':
        compile_fingerprint(args.args_file, args.out, args.profile_dir)
    elif args.command == 'check-inference-report':
        check_inference_report(args.report, args.profile)
    elif args.command == 'build-tools':
        check_build_tools()
    else:
        check_environment()


if __name__ == '__main__':
    main()
