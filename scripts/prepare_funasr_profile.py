"""Generate the reduced FunASR inference package and Nuitka module_code config.

Bounded build step for the "funasr-inference-1.2.6" profile described in
docs/funasr-inference-pruning-design.md. It reads the pinned, installed upstream
sources without importing the whole FunASR package, projects four modules from
reviewed AST nodes, checks that the preserved definitions are byte-for-byte
upstream, and emits a selected source tree plus a Nuitka user package
configuration that fully replaces those four modules at compile time.

Nothing here executes inference or compiles. All guards fail the build; there is
no fallback to recursive inclusion.
"""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import shutil
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / 'release-assets' / 'funasr-inference-profile.json'
MODULES_LIST_NAME = 'funasr-inference-modules.txt'
YAML_NAME = 'funasr-inference.nuitka-package.config.yml'
REPORT_NAME = 'generation-report.json'
GENERATED_MODULES = ('funasr', 'funasr.auto.auto_model', 'funasr.utils.load_utils',
                     'funasr.frontends.wav_frontend')


class ProfileError(RuntimeError):
    """The installed sources or the profile do not satisfy the generation contract."""


def sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def package_source_root():
    distribution = importlib.metadata.distribution('funasr')
    root = Path(distribution.locate_file('funasr'))
    if not (root / '__init__.py').is_file():
        raise ProfileError(f'Installed funasr package is incomplete: {root}')
    return root


def module_source_path(root, module):
    parts = module.split('.')
    if parts[0] != 'funasr':
        raise ProfileError(f'Not a funasr module: {module}')
    tail = parts[1:]
    candidate = root.joinpath(*tail).with_suffix('.py')
    if candidate.is_file():
        return candidate
    package = root.joinpath(*tail, '__init__.py')
    if package.is_file():
        return package
    raise ProfileError(f'FunASR module is missing from the installation: {module}')


def read_source(root, module):
    return module_source_path(root, module).read_text(encoding='utf-8')


def parse(source, description):
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise ProfileError(f'Unable to parse {description}: {error}') from error


def node_source(text, node):
    """Verbatim upstream source for a definition, including its decorators."""
    lines = text.splitlines()
    start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
    return '\n'.join(lines[start - 1:node.end_lineno])


def definition_dump(node):
    return ast.dump(node, include_attributes=False)


def top_level_definitions(tree):
    return {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def find_definition(tree, dotted_name, module):
    """Resolve ``Name`` or ``Class.method`` to exactly one AST node."""
    parts = dotted_name.split('.')
    if len(parts) == 1:
        found = top_level_definitions(tree).get(parts[0])
        if found is None:
            raise ProfileError(f'{module} has no top-level definition {dotted_name!r}')
        return found
    if len(parts) != 2:
        raise ProfileError(f'Unsupported definition path: {dotted_name}')
    owner = top_level_definitions(tree).get(parts[0])
    if not isinstance(owner, ast.ClassDef):
        raise ProfileError(f'{module} has no class {parts[0]!r} for {dotted_name!r}')
    matches = [node for node in owner.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[1]]
    if len(matches) != 1:
        raise ProfileError(
            f'{module} must define exactly one {dotted_name!r}, found {len(matches)}')
    return matches[0]


def check_versions(profile, root):
    expected = profile['runtime']['funasr']
    installed = importlib.metadata.version('funasr')
    if installed != expected:
        raise ProfileError(f'FunASR version mismatch: profile pins {expected}, installed {installed}')
    version_file = root / 'version.txt'
    if not version_file.is_file():
        raise ProfileError(f'FunASR version.txt is missing: {version_file}')
    declared = version_file.read_text(encoding='utf-8').strip()
    if declared != expected:
        raise ProfileError(f'FunASR version.txt mismatch: expected {expected}, found {declared}')
    return declared


def check_source_hashes(profile, root):
    for module, expected in profile['source_hashes'].items():
        actual = sha256_file(module_source_path(root, module))
        if actual != expected:
            raise ProfileError(
                f'Upstream source changed for {module}: expected {expected}, found {actual}. '
                'Review the profile before regenerating; do not accept new hashes automatically.')


INIT_TEMPLATE = '''\
"""ASRTools selected FunASR inference package (profile ${profile})."""
# Generated by scripts/prepare_funasr_profile.py from pinned FunASR ${version}.
# This module replaces upstream recursive `pkgutil.walk_packages` discovery with
# the explicit registrations the local aligner needs. The version metadata read
# is retained. Do not edit; change the profile and regenerate.

import os

dirname = os.path.dirname(__file__)
version_file = os.path.join(dirname, "version.txt")
with open(version_file, "r") as f:
    __version__ = f.read().strip()


import funasr.register  # noqa: F401  (defines the registry tables before registration)
${registration_imports}
from funasr.auto.auto_model import AutoModel  # noqa: F401  (public entry point)

from funasr.register import tables

os.environ["HYDRA_FULL_ERROR"] = "1"

_REQUIRED_REGISTRATIONS = (
${required_registrations}
)

for _table_name, _key, _expected in _REQUIRED_REGISTRATIONS:
    _actual = getattr(tables, _table_name).get(_key)
    if _actual is not _expected:
        raise RuntimeError(
            "FunASR inference profile is incomplete: %s/%s resolved to %r, expected %r"
            % (_table_name, _key, _actual, _expected)
        )
'''

AUTO_MODEL_TEMPLATE = '''\
"""Reduced FunASR AutoModel: local model construction only (profile ${profile})."""
# Generated by scripts/prepare_funasr_profile.py from pinned FunASR ${version}.
# `build_model` is copied verbatim from upstream; only package discovery, the
# speaker-clustering/VAD/punctuation branches and the download path are removed.
# Do not edit; change the profile and regenerate.

import logging
import os

import torch
from omegaconf import ListConfig

from funasr.register import tables
from funasr.train_utils.load_pretrained_model import load_pretrained_model
from funasr.train_utils.set_all_random_seed import set_all_random_seed
from funasr.utils.misc import deep_update

_SELECTED_CONFIGURATION = ${selected_configuration}


def download_model(**kwargs):
    """Explicit failure: this profile never resolves models from a hub."""
    raise RuntimeError(
        "ASRTools ships a local-only FunASR inference profile; model-hub download "
        "is not available. Provide a local model_conf and init_param path."
    )


def _require_local_file(field, value):
    if not isinstance(value, (str, os.PathLike)) or not os.path.isfile(value):
        raise RuntimeError(
            "ASRTools inference profile requires a local %s file, got %r" % (field, value)
        )
    return value


def _validate_local_profile(kwargs):
    """Reject any request outside the pinned local alignment configuration."""
    for name, expected in _SELECTED_CONFIGURATION.items():
        actual = kwargs.get(name)
        if actual != expected:
            raise RuntimeError(
                "ASRTools inference profile supports only %s=%r, got %r"
                % (name, expected, actual)
            )
    if not isinstance(kwargs.get("model_conf"), dict):
        raise RuntimeError("ASRTools inference profile requires a local model_conf mapping")
    _require_local_file("init_param", kwargs.get("init_param"))
    tokenizer_conf = kwargs.get("tokenizer_conf")
    if not isinstance(tokenizer_conf, dict):
        raise RuntimeError("ASRTools inference profile requires a local tokenizer_conf mapping")
    _require_local_file("tokenizer_conf.token_list", tokenizer_conf.get("token_list"))
    _require_local_file("tokenizer_conf.seg_dict_file", tokenizer_conf.get("seg_dict_file"))
    frontend_conf = kwargs.get("frontend_conf")
    if not isinstance(frontend_conf, dict):
        raise RuntimeError("ASRTools inference profile requires a local frontend_conf mapping")
    _require_local_file("frontend_conf.cmvn_file", frontend_conf.get("cmvn_file"))
    if kwargs.get("device") != "cpu":
        raise RuntimeError("ASRTools inference profile runs on CPU only")
    if kwargs.get("disable_update") is not True:
        raise RuntimeError("ASRTools inference profile requires disable_update=True")
    if kwargs.get("trust_remote_code") is not False:
        raise RuntimeError("ASRTools inference profile requires trust_remote_code=False")
    ncpu = kwargs.get("ncpu")
    if isinstance(ncpu, bool) or not isinstance(ncpu, int) or ncpu < 1:
        raise RuntimeError("ASRTools inference profile requires a positive integer ncpu")
    for auxiliary in ("vad_model", "punc_model", "spk_model"):
        if kwargs.get(auxiliary):
            raise RuntimeError(
                "ASRTools inference profile does not build %s" % auxiliary)
    return kwargs


class AutoModel:

${preserved_build_model}

    def __init__(self, **kwargs):
        kwargs = _validate_local_profile(kwargs)
        log_level = getattr(logging, kwargs.get("log_level", "INFO").upper())
        logging.basicConfig(level=log_level)

        model, kwargs = self.build_model(**kwargs)

        self.kwargs = kwargs
        self.model = model
        self.vad_model = None
        self.vad_kwargs = {}
        self.punc_model = None
        self.punc_kwargs = {}
        self.spk_model = None
        self.spk_kwargs = {}
        self.model_path = kwargs.get("model_path")
'''

LOAD_UTILS_TEMPLATE = '''\
"""Reduced FunASR load utilities: `extract_fbank` only (profile ${profile})."""
# Generated by scripts/prepare_funasr_profile.py from pinned FunASR ${version}.
# ASRTools converts media with its own bundled FFmpeg and passes PCM arrays, so
# the general media/URL loader and its librosa/kaldiio/torchaudio backends are
# removed instead of lazily imported. Do not edit; change the profile.

import numpy as np
import torch
from torch.nn.utils.rnn import pad_sequence


${preserved_extract_fbank}


def load_audio_text_image_video(*args, **kwargs):
    """Explicit failure: this profile does not compile the general media loader."""
    raise RuntimeError(
        "ASRTools ships a local-only FunASR inference profile; "
        "load_audio_text_image_video is not available."
    )
'''

WAV_FRONTEND_TEMPLATE = '''\
# Copyright (c) Alibaba, Inc. and its affiliates.
# Part of the implementation is borrowed from espnet/espnet.
"""Reduced FunASR WavFrontend: the selected offline frontend only (profile ${profile})."""
# Generated by scripts/prepare_funasr_profile.py from pinned FunASR ${version}.
# WavFrontendOnline, WavFrontendMel23 and the EEND-only feature dependency are
# removed; the retained definitions are copied verbatim. Do not edit.

from typing import Tuple

import numpy as np
import torch
import torch.nn as nn
import torchaudio.compliance.kaldi as kaldi
from torch.nn.utils.rnn import pad_sequence

from funasr.register import tables


${preserved_load_cmvn}


${preserved_apply_cmvn}


${preserved_apply_lfr}


${preserved_wav_frontend}
'''


def registration_imports(profile):
    lines = [f'from {entry["module"]} import {entry["symbol"]}'
             for entry in profile['registry']]
    return '\n'.join(lines)


def required_registrations(profile):
    lines = [f'    ({entry["table"]!r}, {entry["key"]!r}, {entry["symbol"]}),'
             for entry in profile['registry']]
    return '\n'.join(lines)


def project_init(profile, version):
    return string.Template(INIT_TEMPLATE).substitute(
        profile=profile['profile'], version=version,
        registration_imports=registration_imports(profile),
        required_registrations=required_registrations(profile),
    )


def project_auto_model(profile, version, root):
    module = 'funasr.auto.auto_model'
    text = read_source(root, module)
    node = find_definition(parse(text, module), 'AutoModel.build_model', module)
    # The upstream method already sits at class-body indentation; insert verbatim.
    return string.Template(AUTO_MODEL_TEMPLATE).substitute(
        profile=profile['profile'], version=version,
        selected_configuration=json.dumps(profile['selected_configuration'], ensure_ascii=False,
                                          sort_keys=True),
        preserved_build_model=node_source(text, node),
    ), node


def project_load_utils(profile, version, root):
    module = 'funasr.utils.load_utils'
    text = read_source(root, module)
    node = find_definition(parse(text, module), 'extract_fbank', module)
    return string.Template(LOAD_UTILS_TEMPLATE).substitute(
        profile=profile['profile'], version=version,
        preserved_extract_fbank=node_source(text, node),
    ), node


def project_wav_frontend(profile, version, root):
    module = 'funasr.frontends.wav_frontend'
    text = read_source(root, module)
    tree = parse(text, module)
    nodes = {name: find_definition(tree, name, module)
             for name in ('load_cmvn', 'apply_cmvn', 'apply_lfr', 'WavFrontend')}
    return string.Template(WAV_FRONTEND_TEMPLATE).substitute(
        profile=profile['profile'], version=version,
        preserved_load_cmvn=node_source(text, nodes['load_cmvn']),
        preserved_apply_cmvn=node_source(text, nodes['apply_cmvn']),
        preserved_apply_lfr=node_source(text, nodes['apply_lfr']),
        preserved_wav_frontend=node_source(text, nodes['WavFrontend']),
    ), nodes


def generated_modules(profile, version, root):
    """Return ``{module: (source, preserved_nodes)}`` for the four projections."""
    auto_model, auto_node = project_auto_model(profile, version, root)
    load_utils, fbank_node = project_load_utils(profile, version, root)
    wav_frontend, frontend_nodes = project_wav_frontend(profile, version, root)
    return {
        'funasr': (project_init(profile, version), {}),
        'funasr.auto.auto_model': (auto_model, {'AutoModel.build_model': auto_node}),
        'funasr.utils.load_utils': (load_utils, {'extract_fbank': fbank_node}),
        'funasr.frontends.wav_frontend': (wav_frontend, dict(frontend_nodes)),
    }


def check_preserved_definitions(profile, root, generated):
    """Parse the generated modules and require unchanged ASTs for preserved nodes."""
    evidence = {}
    for module, names in profile['retained_definitions'].items():
        source, _ = generated[module]
        generated_tree = parse(source, f'generated {module}')
        upstream_text = read_source(root, module)
        upstream_tree = parse(upstream_text, module)
        for name in names:
            upstream = find_definition(upstream_tree, name, module)
            produced = find_definition(generated_tree, name, f'generated {module}')
            if definition_dump(upstream) != definition_dump(produced):
                raise ProfileError(
                    f'Generated {module}:{name} differs from upstream; refusing to emit')
            evidence[f'{module}:{name}'] = sha256_text(node_source(upstream_text, upstream))
    for module, (source, _) in generated.items():
        parse(source, f'generated {module}')
    return evidence


def is_funasr_module(root, module):
    try:
        module_source_path(root, module)
        return True
    except ProfileError:
        return False


def imported_funasr_modules(source, module, root):
    """Static funasr imports from one module's source (absolute imports only)."""
    imports = set()
    for node in ast.walk(parse(source, module)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'funasr' or alias.name.startswith('funasr.'):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ProfileError(f'{module} uses a relative import; the profile expects absolute imports')
            if not node.module or not (node.module == 'funasr' or node.module.startswith('funasr.')):
                continue
            imports.add(node.module)
            # `from package import name` also initializes package.name when it is a module.
            for alias in node.names:
                candidate = f'{node.module}.{alias.name}'
                if is_funasr_module(root, candidate):
                    imports.add(candidate)
    return imports


def with_ancestors(modules):
    expanded = set()
    for module in modules:
        parts = module.split('.')
        for index in range(1, len(parts) + 1):
            expanded.add('.'.join(parts[:index]))
    return expanded


def resolve_closure(profile, root, generated, module_list):
    """Walk the selected modules and report the effective funasr import closure."""
    accepted = set(profile['module_closure'])
    sources = {module: generated[module][0] if module in generated else read_source(root, module)
               for module in module_list}
    pending = list(profile['closure_roots'])
    seen = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        if module not in sources:
            sources[module] = read_source(root, module)
        for imported in imported_funasr_modules(sources[module], module, root):
            module_source_path(root, imported)  # fail early on a missing upstream module
            # Importing a nested module also initializes its package ancestors.
            pending.extend(with_ancestors([imported]))
    computed = with_ancestors(seen)
    unexpected = sorted(computed - accepted)
    missing = sorted(accepted - computed)
    if unexpected:
        raise ProfileError(
            'Import closure contains modules outside the reviewed profile: '
            f'{unexpected}. Inspect their importer before editing the closure list.')
    external = sorted({
        alias.split('.')[0]
        for module in seen
        for alias in _external_imports(sources[module], module)
    })
    return sorted(accepted), {'computed_modules': sorted(computed),
                              'unexpected_modules': unexpected,
                              'reviewed_but_not_observed': missing,
                              'external_dependencies': external}


def _external_imports(source, module):
    found = set()
    for node in ast.walk(parse(source, module)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module)
    return {name for name in found if name != 'funasr' and not name.startswith('funasr.')}


def write_selected_tree(root, profile, generated, destination):
    """Emit accepted funasr sources with the four generated projections substituted."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for module in profile['module_closure']:
        source_path = module_source_path(root, module)
        relative = source_path.relative_to(root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if module in generated:
            target.write_text(generated[module][0], encoding='utf-8')
        else:
            shutil.copy2(source_path, target)
    version_source = root / 'version.txt'
    if not version_source.is_file():
        raise ProfileError(f'FunASR version.txt is missing: {version_source}')
    shutil.copy2(version_source, destination / 'version.txt')
    return destination


def yaml_block(text, indent):
    pad = ' ' * indent
    lines = text.rstrip('\n').split('\n')
    return '\n'.join(pad + line if line.strip() else '' for line in lines)


def nuitka_yaml(profile, generated):
    """Emit a Nuitka user package configuration with four full module replacements."""
    chunks = []
    for module, (source, _) in generated.items():
        chunks.append(
            f"- module-name: '{module}'\n"
            "  anti-bloat:\n"
            f"    - description: 'ASRTools inference profile {profile['profile']}'\n"
            "      module_code: |\n"
            f"{yaml_block(source, 8)}\n"
            "      when: 'standalone'\n"
        )
    return '\n'.join(chunks)


def parse_and_check_yaml(text, profile, generated):
    """Parse the emitted YAML back and require the expected full replacements."""
    try:
        import yaml
    except ImportError as error:
        raise ProfileError('PyYAML is required to verify the generated configuration') from error
    document = yaml.safe_load(text)
    if not isinstance(document, list) or len(document) != len(generated):
        raise ProfileError('Generated Nuitka configuration must be a list of four module entries')
    seen = {}
    for entry in document:
        name = entry.get('module-name')
        anti_bloat = entry.get('anti-bloat')
        if not isinstance(anti_bloat, list) or len(anti_bloat) != 1:
            raise ProfileError(f'Generated entry for {name} must have one anti-bloat rule')
        seen[name] = anti_bloat[0]
    if set(seen) != set(generated):
        raise ProfileError(f'Generated Nuitka configuration covers the wrong modules: {sorted(seen)}')
    for module, rule in seen.items():
        if rule.get('module_code') != generated[module][0]:
            raise ProfileError(f'Generated module_code for {module} changed when serialized')
    return document


def run(profile_path=PROFILE_PATH, output_dir=None, source_root=None):
    profile_path = Path(profile_path)
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    if profile.get('schema') != 1:
        raise ProfileError(f'Unsupported profile schema: {profile.get("schema")!r}')
    root = Path(source_root) if source_root else package_source_root()
    version = check_versions(profile, root)
    check_source_hashes(profile, root)

    generated = generated_modules(profile, version, root)
    preserved = check_preserved_definitions(profile, root, generated)

    if output_dir is None:
        raise ProfileError('An output directory is required')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    write_selected_tree(root, profile, generated, output_dir / 'funasr')

    module_list = list(profile['module_closure'])
    (output_dir / MODULES_LIST_NAME).write_text('\n'.join(module_list) + '\n', encoding='utf-8')

    yaml_text = nuitka_yaml(profile, generated)
    parse_and_check_yaml(yaml_text, profile, generated)
    (output_dir / YAML_NAME).write_text(yaml_text, encoding='utf-8')

    resolved, closure_report = resolve_closure(profile, root, generated, module_list)

    report = {
        'profile': profile['profile'],
        'profile_sha256': sha256_file(profile_path),
        'funasr_version': version,
        'source_hashes': profile['source_hashes'],
        'generated_module_sha256': {module: sha256_text(source)
                                    for module, (source, _) in generated.items()},
        'preserved_definition_sha256': preserved,
        'module_list': resolved,
        'closure': closure_report,
        'nuitka_config_sha256': sha256_text(yaml_text),
        'runtime_checks': 'not run: packaged equivalence is a separate user-owned acceptance',
    }
    (output_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'FunASR inference profile: {profile["profile"]} ({len(resolved)} modules) -> {output_dir}')
    print(f'  unexpected modules: {closure_report["unexpected_modules"] or "none"}')
    print(f'  external dependencies: {", ".join(closure_report["external_dependencies"])}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=PROFILE_PATH)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--source-root', type=Path,
                        help='Installed funasr package directory; defaults to the build environment')
    args = parser.parse_args()
    run(args.profile, args.output_dir, args.source_root)


if __name__ == '__main__':
    main()
