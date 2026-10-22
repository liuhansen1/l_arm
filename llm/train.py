"""真实训练四层动作意图分类器，独立保存权重、数据摘要及训练历史。"""

import argparse
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import time

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from llm.dataset import DATA_DIRECTORY, generate
from llm.evaluate import classification_metrics, read_records
from llm.model import GraspLanguageModel, INTENT_CHECKPOINT, IntentParser, ModelConfig, encode_text, text_tokens
from llm.schema import ACTIONS, INTENT_SCHEMA_VERSION, SPECIAL_TOKENS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=45)
    parser.add_argument("--seed", type=int, default=20261022)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=4e-4)
    parser.add_argument("--init-checkpoint", type=Path, help="从已有动作模型继续训练，并按 token 名称迁移嵌入")
    parser.add_argument("--data", type=Path, default=DATA_DIRECTORY)
    parser.add_argument("--checkpoint", type=Path, default=INTENT_CHECKPOINT)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "experiments/intent_v2")
    args = parser.parse_args()
    if args.epochs < 1 or args.threads < 1 or not 0 < args.learning_rate < 1:
        parser.error("epochs、threads 必须大于零，learning-rate 必须在 (0,1) 内")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    present = [(args.data / f"{split}.jsonl").exists() for split in ("train", "validation", "test")]
    if any(present) and not all(present):
        parser.error("数据集不完整，请补齐 train/validation/test.jsonl；不会覆盖已有数据。")
    if not any(present):
        generate(args.data)
    train = read_records(args.data / "train.jsonl")
    validation = read_records(args.data / "validation.jsonl")
    vocabulary = SPECIAL_TOKENS + sorted({t for r in train for t in text_tokens(r["text"])} - set(SPECIAL_TOKENS))
    config = ModelConfig(vocab_size=len(vocabulary), max_length=64, architecture="intent")
    model = GraspLanguageModel(config)
    initialization = None
    if args.init_checkpoint:
        initial = IntentParser(args.init_checkpoint)
        if {k: v for k, v in asdict(initial.config).items() if k != "vocab_size"} != {
                k: v for k, v in asdict(config).items() if k != "vocab_size"}:
            parser.error("初始权重与当前 Encoder 配置不兼容")
        state = model.state_dict()
        state.update({name: value for name, value in initial.model.state_dict().items()
                      if name != "token_embedding.weight"})
        lookup = {token: index for index, token in enumerate(initial.vocab)}
        for index, token in enumerate(vocabulary):
            if token in lookup:
                state["token_embedding.weight"][index] = initial.model.token_embedding.weight[lookup[token]].detach()
        model.load_state_dict(state, strict=True)
        initialization = {"path": str(args.init_checkpoint),
                          "sha256": hashlib.sha256(args.init_checkpoint.read_bytes()).hexdigest()}

    def tensors(records):
        encoded = [encode_text(r["text"], vocabulary, config.max_length) for r in records]
        tokens = torch.tensor(encoded, dtype=torch.long)
        labels = torch.tensor([ACTIONS.index(r["action"]) for r in records], dtype=torch.long)
        return TensorDataset(tokens, labels)

    loader = DataLoader(tensors(train), batch_size=64, shuffle=True)
    valid_loader = DataLoader(tensors(validation), batch_size=128)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=0.01)
    criterion = nn.CrossEntropyLoss()
    best_loss, stale, history = float("inf"), 0, []
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    for epoch in range(1, args.epochs + 1):
        model.train()
        train_loss, train_correct = 0.0, 0
        for tokens, labels in loader:
            length = int(tokens.ne(0).sum(-1).max())
            optimizer.zero_grad(set_to_none=True)
            logits = model(tokens[:, :length])["action"]
            loss = criterion(logits, labels)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item() * len(labels)
            train_correct += int(logits.argmax(-1).eq(labels).sum())
        model.eval()
        valid_loss, valid_correct = 0.0, 0
        with torch.inference_mode():
            for tokens, labels in valid_loader:
                length = int(tokens.ne(0).sum(-1).max())
                logits = model(tokens[:, :length])["action"]
                valid_loss += criterion(logits, labels).item() * len(labels)
                valid_correct += int(logits.argmax(-1).eq(labels).sum())
        row = {"epoch": epoch, "train_loss": train_loss / len(train), "validation_loss": valid_loss / len(validation),
               "train_accuracy": train_correct / len(train), "validation_accuracy": valid_correct / len(validation)}
        history.append(row)
        print(json.dumps(row), flush=True)
        if row["validation_loss"] < best_loss:
            best_loss, stale = row["validation_loss"], 0
            torch.save({"schema_version": INTENT_SCHEMA_VERSION, "actions": ACTIONS, "config": asdict(config),
                        "vocab": vocabulary, "model_state": model.state_dict(), "confidence_threshold": 0.8,
                        "epoch": epoch, "seed": args.seed}, args.checkpoint)
        else:
            stale += 1
        if stale >= 8 and epoch >= 15:
            break
    predictor = IntentParser(args.checkpoint, threshold=0)
    predictions = predictor.predict_many([r["text"] for r in validation])
    candidates = []
    for threshold in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95):
        results = [{**r, "accepted": r["confidence"] >= threshold} for r in predictions]
        metrics = classification_metrics(validation, results)
        candidates.append((threshold, metrics))
    eligible = [r for r in candidates if r[1]["accepted_accuracy"] >= 0.98]
    threshold, metrics = max(eligible, key=lambda r: (r[1]["accepted_coverage"], r[0])) if eligible else candidates[-1]
    saved = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    saved["confidence_threshold"] = threshold
    torch.save(saved, args.checkpoint)
    manifest = {"seed": args.seed, "threads": args.threads, "epochs_completed": len(history), "best_epoch": saved["epoch"],
                "learning_rate": args.learning_rate, "initial_checkpoint": initialization,
                "config": asdict(config), "actions": ACTIONS, "confidence_threshold": threshold,
                "parameters": sum(p.numel() for p in model.parameters()),
                "training_seconds": time.perf_counter() - start,
                "checkpoint_sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
                "datasets": {split: {"sha256": hashlib.sha256((args.data / f"{split}.jsonl").read_bytes()).hexdigest(),
                                     "samples": len(read_records(args.data / f"{split}.jsonl"))}
                             for split in ("train", "validation", "test")}}
    for filename, value in (("manifest.json", manifest), ("history.json", history), ("validation_metrics.json", metrics)):
        (args.output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output / "history.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(history[0]))
        writer.writeheader()
        writer.writerows(history)
    # 独立 HTML 使用浏览器原生 canvas 展示真实历史，不依赖绘图库或外部资源。
    report = '''<!doctype html><meta charset="utf-8"><title>Intent training history</title>
<style>body{font:16px system-ui;margin:32px;background:#f5f7fb;color:#1d2840}canvas{background:white;max-width:100%;border:1px solid #ddd}h2{font-size:18px}</style>
<h1>Four-layer Transformer training</h1><p>Blue: training · Orange: validation</p>
<h2>Loss</h2><canvas id="loss" width="900" height="300"></canvas><h2>Accuracy</h2><canvas id="accuracy" width="900" height="300"></canvas>
<script>const rows=HISTORY;for(const metric of ['loss','accuracy']){const c=document.getElementById(metric),x=c.getContext('2d');const max=metric==='accuracy'?1:Math.max(...rows.flatMap(r=>[r.train_loss,r.validation_loss]));x.strokeStyle='#ddd';x.strokeRect(50,20,820,240);for(let i=0;i<=4;i++){x.fillText((max*i/4).toFixed(2),5,265-i*60)}x.fillText('Epoch',815,292);for(const [field,color]of[['train_','#2874ed'],['validation_','#e98520']]){x.strokeStyle=color;x.beginPath();rows.forEach((r,i)=>{const px=50+i*820/Math.max(1,rows.length-1),py=260-r[field+metric]/max*240;i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}rows.forEach((r,i)=>{if(i%5===0)x.fillText(r.epoch,50+i*820/Math.max(1,rows.length-1),280)})}</script>'''
    (args.output / "training.html").write_text(report.replace("HISTORY", json.dumps(history)), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
