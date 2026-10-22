"""四层 Transformer 动作意图分类器、占位符编码与推理入口。"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time

import torch
from torch import nn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.schema import ACTIONS, INTENT_SCHEMA_VERSION, normalize_text

INTENT_CHECKPOINT = Path(__file__).resolve().parent / "checkpoints/intent_v2.pt"
DEFAULT_CHECKPOINT = INTENT_CHECKPOINT


def text_tokens(text):
    return re.findall(r"<OBJ>|<TASK>|.", normalize_text(text))


@dataclass
class ModelConfig:
    vocab_size: int
    max_length: int = 128
    d_model: int = 192
    num_layers: int = 4
    num_heads: int = 4
    dim_feedforward: int = 768
    dropout: float = 0.1
    architecture: str = "intent"


class GraspLanguageModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.token_embedding = nn.Embedding(config.vocab_size, config.d_model, padding_idx=0)
        self.position_embedding = nn.Embedding(config.max_length, config.d_model)
        self.dropout = nn.Dropout(config.dropout)
        layer = nn.TransformerEncoderLayer(
            d_model=config.d_model, nhead=config.num_heads,
            dim_feedforward=config.dim_feedforward, dropout=config.dropout,
            activation="gelu", batch_first=True, norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(
            layer, config.num_layers, norm=nn.LayerNorm(config.d_model),
            enable_nested_tensor=False,
        )
        self.heads = nn.ModuleDict({"action": nn.Linear(config.d_model, len(ACTIONS))})
        if config.architecture != "intent":
            raise ValueError("只支持固定动作意图分类结构")
        # TransformerEncoder 会复制初始层；重新初始化使各层起点独立。
        self.apply(self._initialize)
        with torch.no_grad():
            self.token_embedding.weight[0].zero_()

    @staticmethod
    def _initialize(module):
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, std=0.02)
        elif isinstance(module, nn.MultiheadAttention):
            nn.init.xavier_uniform_(module.in_proj_weight)
            if module.in_proj_bias is not None:
                nn.init.zeros_(module.in_proj_bias)

    def forward(self, tokens):
        positions = torch.arange(tokens.shape[1], device=tokens.device)
        hidden = self.dropout(self.token_embedding(tokens) + self.position_embedding(positions))
        hidden = self.encoder(hidden, src_key_padding_mask=tokens.eq(0))
        return {"action": self.heads["action"](hidden[:, 0])}


def encode_text(text, vocab, max_length):
    text = normalize_text(text)
    if not text:
        raise ValueError("指令不能为空")
    pieces = text_tokens(text)
    if len(pieces) + 2 > max_length:
        raise ValueError(f"指令过长，最多支持 {max_length - 2} 个 token")
    lookup = {char: i for i, char in enumerate(vocab)}
    ids = [2] + [lookup.get(c, 1) for c in pieces] + [3]
    return ids + [0] * (max_length - len(ids))


class IntentParser:
    """网页用的七分类模型，复用现有 Encoder 与编码代码。"""

    def __init__(self, checkpoint=INTENT_CHECKPOINT, device="cpu", threshold=None):
        self.device = torch.device(device)
        self.checkpoint = Path(checkpoint)
        saved = torch.load(self.checkpoint, map_location="cpu", weights_only=True)
        if (saved.get("schema_version") != INTENT_SCHEMA_VERSION
                or saved.get("actions") != ACTIONS):
            raise ValueError("动作模型协议与当前代码不一致，请运行 llm.train")
        configuration = dict(saved["config"])
        configuration.pop("color_token_ids", None)
        self.config = ModelConfig(**configuration)
        if self.config.architecture != "intent" or self.config.num_layers != 4:
            raise ValueError("网页模型必须使用四层动作分类 Encoder")
        self.vocab = saved["vocab"]
        self.model = GraspLanguageModel(self.config)
        self.model.load_state_dict(saved["model_state"], strict=True)
        self.model.to(self.device).eval()
        self.threshold = float(saved.get("confidence_threshold", 0.8) if threshold is None else threshold)
        if not 0 <= self.threshold <= 1:
            raise ValueError("置信度阈值必须在 0 到 1 之间")

    @torch.inference_mode()
    def predict_many(self, texts, batch_size=128):
        results = []
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]
            encoded = [encode_text(t, self.vocab, self.config.max_length) for t in batch]
            length = max(sum(i != 0 for i in ids) for ids in encoded)
            tokens = torch.tensor(encoded, dtype=torch.long, device=self.device)[:, :length]
            probabilities = self.model(tokens)["action"].softmax(-1)
            confidence, predicted = probabilities.max(-1)
            for ids, text, score, index in zip(encoded, batch, confidence.tolist(), predicted.tolist()):
                # 仅保留诊断信息；背景里的生字不作为拒绝整句指令的依据。
                unknown_ratio = ids.count(1) / max(1, len(text_tokens(text)))
                reason = None
                if score < self.threshold:
                    reason = "模型置信度不足，请换一种明确的表达。"
                results.append({"action": ACTIONS[index], "confidence": score,
                                "accepted": reason is None, "reason": reason,
                                "unknown_ratio": unknown_ratio})
        return results

    def predict(self, text):
        return self.predict_many([text])[0]


TaskParser = IntentParser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction", nargs="?")
    parser.add_argument("--checkpoint", type=Path, default=INTENT_CHECKPOINT)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threshold", type=float)
    args = parser.parse_args()
    torch.set_num_threads(4)
    try:
        predictor = IntentParser(args.checkpoint, args.device, args.threshold)
        text = args.instruction if args.instruction is not None else input("请输入抓取指令：")
        start = time.perf_counter()
        result = predictor.predict(text)
        if args.device == "cuda":
            torch.cuda.synchronize()
        result["inference_ms"] = (time.perf_counter() - start) * 1000
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["accepted"] else 2
    except (ValueError, OSError, RuntimeError) as error:
        print(f"解析失败：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
