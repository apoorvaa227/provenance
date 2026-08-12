"""Drive the question stream through a context layer and record what came back.

In-process by default, which keeps the loop fast enough to run on every edit.
`--service http://host:port` drives the HTTP service instead, over the same
envelope, so the thing that gets scored locally is the thing that would be
scored over the wire.

The transcript is the only artefact. Scoring reads it and nothing else — the
runner has no opinion about whether an answer was any good, and cannot leak
one into the score by having formed one.

    python -m evals.run --out runs/latest
    python -m evals.run --out runs/http --service http://localhost:8080
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_questions(path: str) -> list[dict]:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def in_process(catalog_path: str, mode: str):
    from contextlayer.agents import Ecosystem
    from contextlayer.catalog import Catalog

    catalog = Catalog(json.loads(Path(catalog_path).read_text(encoding="utf-8")))
    classifier = None
    if mode == "llm":
        from contextlayer.llm import ModelClassifier
        classifier = ModelClassifier()
    eco = Ecosystem(catalog, classifier=classifier)
    return eco, lambda env: eco.answer(env)


def over_http(service: str):
    def call(env: dict) -> dict:
        req = urllib.request.Request(
            f"{service.rstrip('/')}/answer",
            data=json.dumps(env).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    return None, call


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default="data/catalog.json")
    ap.add_argument("--questions", default="data/questions.jsonl")
    ap.add_argument("--out", default="runs/latest")
    ap.add_argument("--service", default=None,
                    help="drive the HTTP service instead of running in-process")
    ap.add_argument("--mode", default="deterministic",
                    choices=["deterministic", "llm", "naive"],
                    help="which arm of the comparison to run")
    args = ap.parse_args()

    questions = load_questions(args.questions)
    if args.service:
        eco, call = over_http(args.service)
    elif args.mode == "naive":
        from evals.naive import NaiveAgent
        agent = NaiveAgent(args.catalog)
        eco, call = None, agent.answer
    else:
        eco, call = in_process(args.catalog, args.mode)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.jsonl"

    latencies, failures = [], 0
    with transcript.open("w", encoding="utf-8") as fh:
        for i, q in enumerate(questions, start=1):
            started = time.perf_counter()
            try:
                response = call(q)
                error = None
            except Exception as e:                     # noqa: BLE001
                # A question that raises still has to produce a record. One
                # bad question cannot be allowed to cost the rest of the run.
                response, error = None, f"{type(e).__name__}: {e}"
                failures += 1
            elapsed = time.perf_counter() - started
            latencies.append(elapsed)
            fh.write(json.dumps({"question_id": q["question_id"],
                                 "prompt": q["prompt"],
                                 "asset_id": q.get("asset_id"),
                                 "response": response,
                                 "error": error,
                                 "latency_s": round(elapsed, 4)}) + "\n")
            # Flush per answer. A model-backed run takes minutes against a
            # rate-limited free tier, and a buffered transcript makes it
            # indistinguishable from a hung one — which is how you end up
            # reading a stale file from a previous run and believing it.
            fh.flush()
            if i % 10 == 0 or i == len(questions):
                print(f"  {i}/{len(questions)}", flush=True)

    usage = {"mode": args.mode, "questions": len(questions),
             "transport_failures": failures,
             "p95_latency_s": round(sorted(latencies)[int(len(latencies) * .95)
                                                      - 1], 4) if latencies else 0}
    if eco is not None and hasattr(eco.classify, "usage"):
        usage.update(eco.classify.usage())
    (out / "usage.json").write_text(json.dumps(usage, indent=1),
                                    encoding="utf-8")

    print(f"{len(questions)} questions -> {transcript} "
          f"(p95 {usage['p95_latency_s']}s, {failures} transport failures)")


if __name__ == "__main__":
    main()
