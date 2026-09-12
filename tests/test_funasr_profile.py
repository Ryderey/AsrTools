"""Guards for the generated FunASR inference profile.

G0: generation must fail on changed upstream hashes, missing definitions and
unreviewed imports, and must preserve retained definitions byte-for-byte.
G1: the generated source tree must produce the same inference numbers as the
locked original installation in a separate fresh process. G1 runs only where the
alignment runtime (torch, funasr, fa-zh model, sample audio) is available.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

import argparse


REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prepare_funasr_profile", REPO_ROOT / "scripts" / "prepare_funasr_profile.py"
)
prepare_funasr_profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prepare_funasr_profile)

PROFILE_PATH = REPO_ROOT / "release-assets" / "funasr-inference-profile.json"
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
MODEL_DIR = REPO_ROOT / "models" / "fa-zh"
AUDIO = REPO_ROOT / "models" / "sensevoice-small-int8" / "test_wavs" / "zh.wav"
TEXT = "开饭时间早上九点至下午五点"
THREADS = 4


def has_installed_funasr():
    return (importlib.util.find_spec("funasr") is not None
            and importlib.util.find_spec("yaml") is not None)


def has_inference_runtime():
    return (has_installed_funasr()
            and importlib.util.find_spec("torch") is not None
            and (MODEL_DIR / "model.pt").is_file()
            and AUDIO.is_file())


FAKE_SELECTED = """\
class FakeAligner:
    pass


class FakeEncoder:
    pass


class FakePredictor:
    pass


class FakeFrontend:
    def output_size(self):
        return 80


class FakeTokenizer:
    pass


class FakeSpecAug:
    pass
"""

FAKE_AUTO_MODEL = """\
import logging

from funasr.register import tables


class AutoModel:

    @staticmethod
    def build_model(**kwargs):
        assert "model" in kwargs
        model_class = tables.model_classes.get(kwargs["model"])
        return model_class(), kwargs
"""

FAKE_LOAD_UTILS = """\
import numpy as np


def extract_fbank(data, data_len=None, data_type: str = "sound", frontend=None, **kwargs):
    if isinstance(data, np.ndarray):
        data = data
    return data, data_len
"""

FAKE_WAV_FRONTEND = """\
from funasr.register import tables


def load_cmvn(cmvn_file):
    return cmvn_file


def apply_cmvn(inputs, cmvn):
    return inputs


def apply_lfr(inputs, lfr_m, lfr_n):
    return inputs


@tables.register("frontend_classes", "WavFrontend")
class WavFrontend:
    def __init__(self, cmvn_file=None, **kwargs):
        self.cmvn_file = cmvn_file
"""

FAKE_REGISTER = """\
class RegisterTables:
    model_classes = {}
    encoder_classes = {}
    predictor_classes = {}
    frontend_classes = {}
    tokenizer_classes = {}
    specaug_classes = {}


tables = RegisterTables()
"""

FAKE_MISC = """\
def deep_update(base, update):
    return base
"""

FAKE_SEED = """\
def set_all_random_seed(seed):
    return seed
"""

FAKE_PRETRAINED = """\
def load_pretrained_model(**kwargs):
    return None
"""

FAKE_INIT = """\
import os

__version__ = "1.2.6"
"""


def write_fake_package(package_root):
    """Write a minimal fake funasr package; ``package_root`` is the funasr directory."""
    files = {
        "__init__.py": FAKE_INIT,
        "version.txt": "1.2.6\n",
        "register.py": FAKE_REGISTER,
        "selected.py": FAKE_SELECTED,
        "auto/__init__.py": "",
        "auto/auto_model.py": FAKE_AUTO_MODEL,
        "utils/__init__.py": "",
        "utils/load_utils.py": FAKE_LOAD_UTILS,
        "utils/misc.py": FAKE_MISC,
        "train_utils/__init__.py": "",
        "train_utils/load_pretrained_model.py": FAKE_PRETRAINED,
        "train_utils/set_all_random_seed.py": FAKE_SEED,
        "frontends/__init__.py": "",
        "frontends/wav_frontend.py": FAKE_WAV_FRONTEND,
    }
    for relative, text in files.items():
        path = package_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return package_root


def fake_profile(root, **overrides):
    profile = {
        "schema": 1,
        "profile": "funasr-inference-test",
        "runtime": {"funasr": "1.2.6"},
        "source_hashes": {
            module: prepare_funasr_profile.sha256_file(
                prepare_funasr_profile.module_source_path(root, module))
            for module in ("funasr", "funasr.auto.auto_model", "funasr.utils.load_utils",
                           "funasr.frontends.wav_frontend")
        },
        "retained_definitions": {
            "funasr.auto.auto_model": ["AutoModel.build_model"],
            "funasr.utils.load_utils": ["extract_fbank"],
            "funasr.frontends.wav_frontend": ["load_cmvn", "apply_cmvn", "apply_lfr", "WavFrontend"],
        },
        "registry": [
            {"table": "model_classes", "key": "FakeAligner",
             "module": "funasr.selected", "symbol": "FakeAligner"},
            {"table": "encoder_classes", "key": "FakeEncoder",
             "module": "funasr.selected", "symbol": "FakeEncoder"},
            {"table": "predictor_classes", "key": "FakePredictor",
             "module": "funasr.selected", "symbol": "FakePredictor"},
            {"table": "frontend_classes", "key": "FakeFrontend",
             "module": "funasr.selected", "symbol": "FakeFrontend"},
            {"table": "tokenizer_classes", "key": "FakeTokenizer",
             "module": "funasr.selected", "symbol": "FakeTokenizer"},
            {"table": "specaug_classes", "key": "FakeSpecAug",
             "module": "funasr.selected", "symbol": "FakeSpecAug"},
        ],
        "selected_configuration": {
            "model": "FakeAligner", "encoder": "FakeEncoder", "predictor": "FakePredictor",
            "frontend": "FakeFrontend", "tokenizer": "FakeTokenizer", "specaug": "FakeSpecAug",
        },
        "module_closure": [
            "funasr", "funasr.register", "funasr.selected",
            "funasr.auto", "funasr.auto.auto_model",
            "funasr.utils", "funasr.utils.load_utils", "funasr.utils.misc",
            "funasr.train_utils", "funasr.train_utils.load_pretrained_model",
            "funasr.train_utils.set_all_random_seed",
            "funasr.frontends", "funasr.frontends.wav_frontend",
        ],
        "closure_roots": [
            "funasr.auto.auto_model", "funasr.utils.load_utils", "funasr.frontends.wav_frontend",
        ],
    }
    profile.update(overrides)
    return profile


class ProfileGenerationGuardTests(unittest.TestCase):
    def _run(self, profile, root, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        path = output / "profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        return prepare_funasr_profile.run(path, output, root)

    def test_changed_upstream_hash_stops_generation(self):
        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "site-packages" / "funasr")
            profile = fake_profile(root)
            profile["source_hashes"]["funasr.utils.load_utils"] = "0" * 64
            with self.assertRaisesRegex(prepare_funasr_profile.ProfileError,
                                        "Upstream source changed for funasr.utils.load_utils"):
                self._run(profile, root, Path(directory) / "out")

    def test_missing_retained_definition_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "funasr")
            (root / "auto" / "auto_model.py").write_text(
                "class AutoModel:\n    pass\n", encoding="utf-8")
            profile = fake_profile(root)
            with self.assertRaisesRegex(prepare_funasr_profile.ProfileError,
                                        "AutoModel"):
                self._run(profile, root, Path(directory) / "out")

    def test_definition_changed_during_projection_is_rejected(self):
        import ast

        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "funasr")
            profile = fake_profile(root)
            real = prepare_funasr_profile.node_source

            def tampering(source, item):
                text = real(source, item)
                if isinstance(item, ast.FunctionDef) and item.name == "extract_fbank":
                    return text + "\n    data = data"
                return text

            prepare_funasr_profile.node_source = tampering
            try:
                generated = prepare_funasr_profile.generated_modules(profile, "1.2.6", root)
                with self.assertRaisesRegex(prepare_funasr_profile.ProfileError,
                                            "differs from upstream"):
                    prepare_funasr_profile.check_preserved_definitions(profile, root, generated)
            finally:
                prepare_funasr_profile.node_source = real

    def test_unreviewed_import_fails_the_closure_gate(self):
        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "funasr")
            # A retained (non-generated) module gains an import outside the reviewed list.
            (root / "utils" / "misc.py").write_text(
                "from funasr.utils import helper\n\n\ndef deep_update(base, update):\n"
                "    return helper.deep_update(base, update)\n",
                encoding="utf-8")
            (root / "utils" / "helper.py").write_text("def deep_update(base, update):\n    return base\n",
                                                      encoding="utf-8")
            profile = fake_profile(root)
            with self.assertRaisesRegex(prepare_funasr_profile.ProfileError,
                                        "outside the reviewed profile: \\['funasr.utils.helper'\\]"):
                self._run(profile, root, Path(directory) / "out")

    def test_generation_is_deterministic_and_replaces_the_four_modules(self):
        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "funasr")
            profile = fake_profile(root)
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            report = self._run(profile, root, first)
            self._run(profile, root, second)
            self.assertEqual(report["closure"]["unexpected_modules"], [])
            for module in prepare_funasr_profile.GENERATED_MODULES:
                relative = prepare_funasr_profile.module_source_path(root, module).relative_to(root)
                self.assertEqual((first / "funasr" / relative).read_bytes(),
                                 (second / "funasr" / relative).read_bytes(), module)
            upstream = prepare_funasr_profile.read_source(root, "funasr.utils.load_utils")
            generated = (first / "funasr" / "utils" / "load_utils.py").read_text(encoding="utf-8")
            self.assertNotEqual(upstream, generated)
            self.assertIn("def extract_fbank", generated)

    def test_generated_yaml_is_a_full_module_replacement(self):
        import yaml

        with TemporaryDirectory() as directory:
            root = write_fake_package(Path(directory) / "funasr")
            profile = fake_profile(root)
            generated = prepare_funasr_profile.generated_modules(profile, "1.2.6", root)
            document = prepare_funasr_profile.nuitka_yaml(profile, generated)
            prepare_funasr_profile.parse_and_check_yaml(document, profile, generated)
            parsed = yaml.safe_load(document)
            self.assertEqual({entry["module-name"] for entry in parsed},
                             set(prepare_funasr_profile.GENERATED_MODULES))
            tampered = document.replace("import os", "import os  # modified", 1)
            with self.assertRaisesRegex(prepare_funasr_profile.ProfileError, "changed when serialized"):
                prepare_funasr_profile.parse_and_check_yaml(tampered, profile, generated)


@unittest.skipUnless(has_installed_funasr(), "requires the locked funasr installation")
class RealProfileTests(unittest.TestCase):
    def test_real_profile_generates_the_reviewed_closure(self):
        with TemporaryDirectory() as directory:
            report = prepare_funasr_profile.run(PROFILE_PATH, Path(directory))
            self.assertEqual(report["funasr_version"], "1.2.6")
            self.assertEqual(report["closure"]["unexpected_modules"], [])
            self.assertEqual(report["closure"]["reviewed_but_not_observed"], [])
            self.assertEqual(sorted(report["module_list"]), sorted(PROFILE["module_closure"]))
            for dependency in ("librosa", "numba", "llvmlite", "scikit-learn", "scipy", "jieba"):
                self.assertNotIn(dependency, report["closure"]["external_dependencies"])
            for module in prepare_funasr_profile.GENERATED_MODULES:
                relative = prepare_funasr_profile.module_source_path(
                    prepare_funasr_profile.package_source_root(), module).relative_to(
                        prepare_funasr_profile.package_source_root())
                (Path(directory) / "funasr" / relative).read_text(encoding="utf-8")


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"Expected 16-bit mono WAV: {path}")
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    import numpy as np
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0, rate


def tensor_digest(tensor):
    import hashlib
    return hashlib.sha256(tensor.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def inference_fingerprint(shadow_root=None):
    """Run the selected alignment path and return comparable numerics.

    With ``shadow_root`` the generated package tree must be the loaded funasr
    package; falling back to the installed tree is a failure.
    """
    if shadow_root:
        sys.path.insert(0, str(shadow_root))
    import numpy as np
    import torch

    import funasr
    from funasr.utils.load_utils import extract_fbank
    from funasr.utils.timestamp_tools import ts_prediction_lfr6_standard
    from bk_asr.offline_alignment import load_aligner, character_intervals

    loaded_from = Path(funasr.__file__).resolve()
    if shadow_root:
        expected = (Path(shadow_root) / "funasr" / "__init__.py").resolve()
        if loaded_from != expected:
            raise SystemExit(f"funasr loaded from {loaded_from}, expected the generated tree {expected}")

    samples, rate = read_wav(AUDIO)
    if rate != 16000 or not len(samples):
        raise SystemExit("equivalence harness requires nonempty 16 kHz mono PCM16 audio")
    model, model_hashes, _ = load_aligner(MODEL_DIR, THREADS)
    tokenizer = model.kwargs["tokenizer"]
    tokens = tokenizer.ids2tokens(tokenizer.encode(TEXT))
    with torch.inference_mode():
        features, lengths = extract_fbank([samples], data_type="sound",
                                         frontend=model.kwargs["frontend"])
        encoded, encoded_lengths = model.model.encode(features, lengths)
        target_lengths = torch.tensor([len(tokens) + 1], device=encoded.device)
        _, _, alphas, peaks = model.model.calc_predictor_timestamp(
            encoded, encoded_lengths, target_lengths)
        frames = int(encoded_lengths[0]) * 3
        _, spans = ts_prediction_lfr6_standard(alphas[0, :frames], peaks[0, :frames], list(tokens))
    spans_final = character_intervals(model, samples, TEXT)

    absent = sorted(name for name in ("librosa", "numba", "llvmlite", "scipy", "sklearn",
                                      "jieba", "funasr.models.campplus")
                    if name in sys.modules)
    return {
        "numerics": {
            "audio_samples": int(len(samples)),
            "model_hashes": model_hashes,
            "tokens": list(tokens),
            "frontend_class": type(model.kwargs["frontend"]).__name__,
            "frontend_dither": float(model.kwargs["frontend"].dither),
            "tokenizer_class": type(tokenizer).__name__,
            "feature_shape": list(features.shape),
            "feature_dtype": str(features.dtype),
            "feature_digest": tensor_digest(features),
            "lengths": [int(value) for value in lengths],
            "encoded_shape": list(encoded.shape),
            "encoded_digest": tensor_digest(encoded),
            "alpha_digest": tensor_digest(alphas),
            "peak_digest": tensor_digest(peaks),
            "predicted_spans": [[int(a), int(b)] for a, b in spans],
            "final_spans": [[int(a), int(b)] for a, b in spans_final],
        },
        "environment": {
            "funasr_file": str(loaded_from),
            "unexpected_modules_loaded": absent,
        },
    }


@unittest.skipUnless(has_inference_runtime(), "requires torch, funasr, fa-zh model and sample audio")
class SourceProfileEquivalenceTests(unittest.TestCase):
    def _run_process(self, shadow_root):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["HF_HUB_OFFLINE"] = "1"
        command = [sys.executable, "-m", "tests.test_funasr_profile", "--fingerprint"]
        if shadow_root:
            command.append(str(shadow_root))
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, check=False,
                                   env=environment, timeout=900)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr.decode("utf-8", errors="replace")[-4000:])
        # Upstream funasr prints banners on import; the fingerprint is the last line.
        lines = [line for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
        return json.loads(lines[-1])

    def test_generated_tree_matches_the_locked_installation(self):
        baseline_first = self._run_process(None)
        baseline_second = self._run_process(None)
        self.assertEqual(baseline_first["numerics"], baseline_second["numerics"],
                         "baseline inference is not repeatable; no tolerance can be chosen")

        with TemporaryDirectory() as directory:
            prepare_funasr_profile.run(PROFILE_PATH, Path(directory))
            candidate = self._run_process(directory)

        self.assertEqual(candidate["numerics"], baseline_first["numerics"])
        self.assertEqual(candidate["environment"]["unexpected_modules_loaded"], [])
        self.assertTrue(candidate["environment"]["funasr_file"].startswith(str(directory)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fingerprint", nargs="?", const="", default=None,
                        help="emit the alignment fingerprint; optional generated tree to shadow")
    args = parser.parse_args()
    if args.fingerprint is None:
        parser.error("--fingerprint is required")
    print(json.dumps(inference_fingerprint(args.fingerprint or None), ensure_ascii=False))


if __name__ == "__main__":
    if "--fingerprint" in sys.argv:
        main()
    else:
        unittest.main()
