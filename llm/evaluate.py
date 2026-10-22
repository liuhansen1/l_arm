"""独立动作分类评估；测试集不参与训练选模和阈值选择。"""

import argparse
import json
from pathlib import Path
import time

import torch

from llm.model import INTENT_CHECKPOINT, IntentParser
from llm.schema import ACTIONS


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def classification_metrics(records, predictions):
    matrix = [[0 for _ in ACTIONS] for _ in ACTIONS]
    accepted, accepted_correct = 0, 0
    errors = []
    for row, result in zip(records, predictions):
        truth, predicted = ACTIONS.index(row["action"]), ACTIONS.index(result["action"])
        matrix[truth][predicted] += 1
        if result["accepted"]:
            accepted += 1
            accepted_correct += truth == predicted
        if truth != predicted or not result["accepted"]:
            errors.append({"text": row["text"], "expected": row["action"], **result})
    per_class = {}
    for index, action in enumerate(ACTIONS):
        tp = matrix[index][index]
        precision = tp / max(1, sum(row[index] for row in matrix))
        recall = tp / max(1, sum(matrix[index]))
        per_class[action] = {"precision": precision, "recall": recall,
                             "f1": 2 * precision * recall / max(1e-12, precision + recall),
                             "support": sum(matrix[index])}
    return {"samples": len(records), "accuracy": sum(matrix[i][i] for i in range(len(ACTIONS))) / max(1, len(records)),
            "accepted_coverage": accepted / max(1, len(records)),
            "accepted_accuracy": accepted_correct / max(1, accepted),
            "macro_f1": sum(v["f1"] for v in per_class.values()) / len(ACTIONS),
            "per_class": per_class, "labels": ACTIONS, "confusion_matrix": matrix, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=INTENT_CHECKPOINT)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data/test.jsonl")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "experiments/intent_v2/test_metrics.json")
    args = parser.parse_args()
    torch.set_num_threads(4)
    predictor = IntentParser(args.checkpoint)
    records = read_records(args.data)
    predictor.predict("抓取<OBJ>")
    start = time.perf_counter()
    predictions = predictor.predict_many([r["text"] for r in records])
    metrics = classification_metrics(records, predictions)
    metrics["batch_ms_per_sample"] = (time.perf_counter() - start) * 1000 / len(records)
    timings = []
    for row in records[:30]:
        start = time.perf_counter()
        predictor.predict(row["text"])
        timings.append((time.perf_counter() - start) * 1000)
    metrics["single_mean_ms"] = sum(timings) / len(timings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("errors", "per_class", "confusion_matrix")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
