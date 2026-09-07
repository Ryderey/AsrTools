"""Local-only fa-zh adapter. Predicted boundaries still require human validation."""

import hashlib
import importlib.util
import logging
import os
import time
import unicodedata


def prepare_native_runtime():
    """Prime optional sentencepiece before Qt loads its native libraries on Windows."""
    if os.name == "nt" and importlib.util.find_spec("sentencepiece") is not None:
        try:
            importlib.import_module("sentencepiece")
        except (ImportError, OSError) as exc:
            logging.warning("离线原生运行库未就绪（%s）；手动 B 接口仍可使用", type(exc).__name__)


def digest(path, should_stop=None):
    with path.open("rb") as source:
        if should_stop is None:
            return hashlib.file_digest(source, "sha256").hexdigest()
        value = hashlib.sha256()
        while True:
            if should_stop():
                raise InterruptedError("已取消文件校验")
            chunk = source.read(1024 * 1024)
            if not chunk:
                return value.hexdigest()
            value.update(chunk)


def spoken_text(text):
    indices = [i for i, c in enumerate(text) if not c.isspace()
               and not unicodedata.category(c).startswith("P")]
    return "".join(text[i] for i in indices), indices


def check_spans(spans, duration_ms):
    return bool(spans) and all(
        len(pair) == 2 and 0 <= pair[0] < pair[1] <= duration_ms
        and (i == 0 or spans[i - 1][1] <= pair[0])
        for i, pair in enumerate(spans)
    )


def trim_padded_tail(spans, duration_ms):
    """Remove at most one 60 ms LFR frame of frontend padding, never speech."""
    if spans and spans[-1][0] < duration_ms < spans[-1][1] <= duration_ms + 60:
        spans[-1][1] = duration_ms
    return spans


def load_aligner(model_dir, threads):
    files = {name: model_dir / name for name in (
        "model.pt", "config.yaml", "configuration.json", "tokens.json", "seg_dict", "am.mvn")}
    for path in files.values():
        if not path.is_file():
            raise FileNotFoundError(f"缺少离线对齐模型文件：{path}")
    hashes = {name: digest(path) for name, path in files.items()}
    if hashes["model.pt"] != "f34ede558af831fb504206b25f1c2f27ca2f77753c26c4dd38d03323153b6f73":
        raise ValueError("离线对齐模型校验失败，请重新放置已验证的模型文件")
    import yaml
    from funasr import AutoModel
    config = yaml.safe_load(files["config.yaml"].read_text(encoding="utf-8"))
    config["tokenizer_conf"].update(token_list=str(files["tokens.json"]),
                                    seg_dict_file=str(files["seg_dict"]))
    config["frontend_conf"]["cmvn_file"] = str(files["am.mvn"])
    config.update(init_param=str(files["model.pt"]), device="cpu", ncpu=threads,
                  disable_update=True, disable_pbar=True, trust_remote_code=False,
                  ignore_init_mismatch=False)
    loading = time.perf_counter()
    model = AutoModel(**config)
    model.model.eval()
    return model, hashes, time.perf_counter() - loading


def character_intervals(model, samples, text):
    """Invoke the pinned model's acoustic predictor before English word merging.

    Matches FunASR 1.2.6 MonotonicAligner.inference's feature/encoder/predictor
    sequence, using its helpers directly; no global hook or text interpolation.
    """
    import torch
    from funasr.utils.load_utils import extract_fbank
    from funasr.utils.timestamp_tools import ts_prediction_lfr6_standard

    tokenizer = model.kwargs["tokenizer"]
    tokens = tokenizer.ids2tokens(tokenizer.encode(text))
    if not text or "<unk>" in tokens or tokens != [c.lower() for c in text]:
        raise ValueError("离线逐字对齐存在不支持的字符，未生成不完整时间轴")
    with torch.inference_mode():
        features, lengths = extract_fbank([samples], data_type="sound", frontend=model.kwargs["frontend"])
        encoded, encoded_lengths = model.model.encode(features, lengths)
        target_lengths = torch.tensor([len(tokens) + 1], device=encoded.device)
        _, _, alphas, peaks = model.model.calc_predictor_timestamp(encoded, encoded_lengths, target_lengths)
        frames = int(encoded_lengths[0]) * 3
        _, spans = ts_prediction_lfr6_standard(alphas[0, :frames], peaks[0, :frames], list(tokens))
    trim_padded_tail(spans, len(samples) / 16)
    if len(spans) != len(text) or not check_spans(spans, len(samples) / 16):
        raise ValueError("离线逐字对齐时间轴不完整或越界，请保留文本并检查音频")
    return spans
