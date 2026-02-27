"""Linus — Modal app for SFT training and evaluation.

Usage:
    modal run linus/train.py::train_main
    modal run linus/train.py::train_main --iters 800 --batch-size 32
    modal run linus/train.py::eval_main
    modal run linus/train.py::eval_main --max-examples 50
"""

import json
from pathlib import Path

import modal

app = modal.App("linus")

serve_image = modal.Image.debian_slim(python_version="3.12").pip_install("vllm")

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "torch",
    "transformers>=4.45",
    "datasets",
    "peft>=0.13",
    "trl==0.14.0",
    "wandb",
    "accelerate",
    "sentence-transformers",
)

vol = modal.Volume.from_name("linus-adapters", create_if_missing=True)

WANDB_PROJECT = "linus"
WANDB_ENTITY = "arpandeepk-stanford-university"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
EMBED_MODEL = "Snowflake/snowflake-arctic-embed-xs"
LORA_RANK = 16
LORA_ALPHA = 32

_secrets = [
    modal.Secret.from_name("arpan-wandb-secret"),
    modal.Secret.from_name("arpan-hf-secret"),
]


# ── Training ─────────────────────────────────────────────────────────────


@app.function(
    image=image,
    gpu="H100",
    timeout=3600,
    volumes={"/adapters": vol},
    secrets=_secrets,
)
def train(
    train_jsonl: str,
    val_jsonl: str,
    iters: int = 400,
    batch_size: int = 32,
    lr: float = 2e-5,
    grad_accum: int = 1,
):
    import os
    import time
    from datetime import datetime

    import torch
    import wandb
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

    train_data = [json.loads(line) for line in train_jsonl.strip().split("\n") if line.strip()]
    val_data = [json.loads(line) for line in val_jsonl.strip().split("\n") if line.strip()]
    print(f"Train: {len(train_data)} examples, Val: {len(val_data)} examples")

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        MODEL,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    lora_config = LoraConfig(
        r=LORA_RANK,
        lora_alpha=LORA_ALPHA,
        lora_dropout=0.0,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    def format_chat(example):
        text = tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False
        )
        return {"text": text}

    train_ds = Dataset.from_list(train_data).map(format_chat)
    val_ds = Dataset.from_list(val_data).map(format_chat)

    ts = datetime.now().strftime("%m%d_%H%M")
    run_name = f"qwen0.5b_r{LORA_RANK}_lr{lr:.0e}_{ts}"

    output_dir = "/adapters/latest"
    num_epochs = max(1, (iters * batch_size * grad_accum) / len(train_data))

    training_args = SFTConfig(
        output_dir=output_dir,
        max_steps=iters,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        weight_decay=0.01,
        bf16=True,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=50,
        save_steps=200,
        save_total_limit=3,
        max_seq_length=512,
        dataset_text_field="text",
        report_to="wandb",
        run_name=run_name,
        seed=42,
        dataloader_num_workers=4,
        dataloader_pin_memory=True,
    )

    os.environ["WANDB_PROJECT"] = WANDB_PROJECT
    os.environ["WANDB_ENTITY"] = WANDB_ENTITY

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        processing_class=tokenizer,
    )

    start = time.time()
    print(f"Starting training: {run_name}")
    print(f"  Model: {MODEL} (bf16, no quantization)")
    print(f"  LoRA: rank={LORA_RANK} alpha={LORA_ALPHA} (all linear layers)")
    print(f"  LR: {lr}, Batch: {batch_size}, Iters: {iters}")
    print(f"  ~{num_epochs:.1f} epochs over {len(train_data)} examples")

    trainer.train()
    elapsed = time.time() - start

    final_train_loss = None
    for entry in reversed(trainer.state.log_history):
        if "loss" in entry:
            final_train_loss = entry["loss"]
            break

    trainer.save_model(output_dir)
    tokenizer.save_pretrained(output_dir)

    meta = {
        "model": MODEL,
        "lora_rank": LORA_RANK,
        "lora_alpha": LORA_ALPHA,
        "learning_rate": lr,
        "batch_size": batch_size,
        "iters": iters,
        "training_time_s": elapsed,
        "final_train_loss": final_train_loss,
        "run_name": run_name,
        "train_examples": len(train_data),
        "val_examples": len(val_data),
        "completed_at": datetime.now().isoformat(),
    }
    with open(f"{output_dir}/training_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    vol.commit()

    print(f"\nTraining complete in {elapsed / 60:.1f} min")
    print("Adapters saved to volume 'linus-adapters' at /latest/")

    wandb.finish()
    return meta


# ── Evaluation ───────────────────────────────────────────────────────────


@app.function(
    image=image,
    gpu="A100",
    timeout=1800,
    volumes={"/adapters": vol},
    secrets=_secrets,
)
def evaluate(val_jsonl: str, max_examples: int = 200) -> dict:
    import os

    import torch
    import wandb
    from peft import PeftModel
    from sentence_transformers import SentenceTransformer
    from transformers import AutoModelForCausalLM, AutoTokenizer

    adapter_path = "/adapters/latest"
    if not Path(adapter_path).exists():
        return {"error": "no_adapters", "score": 0, "type_accuracy": 0, "semantic_similarity": 0}

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    base_model = AutoModelForCausalLM.from_pretrained(
        MODEL,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base_model, adapter_path)
    model.eval()

    embedder = SentenceTransformer(EMBED_MODEL, device="cuda")

    examples = [json.loads(line) for line in val_jsonl.strip().split("\n") if line.strip()]
    if len(examples) > max_examples:
        examples = examples[:max_examples]

    generated_list = []
    ground_truths = []

    for ex in examples:
        messages = ex["messages"]
        prompt_messages = [m for m in messages if m["role"] != "assistant"]
        ground_truth = messages[-1]["content"]

        prompt = tokenizer.apply_chat_template(
            prompt_messages, tokenize=False, add_generation_prompt=True
        )
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

        with torch.no_grad():
            output_ids = model.generate(
                **inputs,
                max_new_tokens=64,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )

        generated = tokenizer.decode(
            output_ids[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        ).strip()

        generated_list.append(generated)
        ground_truths.append(ground_truth)

    # Batch-compute semantic similarity
    gen_embeds = embedder.encode(generated_list, normalize_embeddings=True)
    gt_embeds = embedder.encode(ground_truths, normalize_embeddings=True)
    cosine_sims = (gen_embeds * gt_embeds).sum(axis=1).clip(0, 1)

    # Compute type accuracy
    type_matches = 0
    for gen, gt in zip(generated_list, ground_truths):
        gen_type = gen.split("]")[0] + "]" if "]" in gen else ""
        gt_type = gt.split("]")[0] + "]" if "]" in gt else ""
        if gen_type and gen_type == gt_type:
            type_matches += 1

    n = len(examples)
    type_acc = type_matches / n if n else 0
    semantic_sim = float(cosine_sims.mean()) if n else 0
    score = 0.25 * type_acc + 0.75 * semantic_sim

    metrics = {
        "score": score,
        "type_accuracy": type_acc,
        "semantic_similarity": semantic_sim,
        "n_examples": n,
    }

    os.environ.setdefault("WANDB_PROJECT", WANDB_PROJECT)
    os.environ.setdefault("WANDB_ENTITY", WANDB_ENTITY)
    wandb.init(job_type="eval", config={"max_examples": max_examples, "model": MODEL})
    wandb.log(metrics)
    wandb.finish()

    print(json.dumps(metrics, indent=2))
    return metrics


# ── Inference server (vLLM) ──────────────────────────────────────────────


@app.function(
    image=serve_image,
    gpu="A100",
    volumes={"/adapters": vol},
    secrets=[modal.Secret.from_name("arpan-hf-secret")],
    scaledown_window=300,
)
@modal.web_server(port=8000, startup_timeout=300)
def serve():
    import subprocess

    subprocess.Popen(
        [
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            MODEL,
            "--enable-lora",
            "--lora-modules",
            "linus=/adapters/latest",
            "--max-lora-rank",
            str(LORA_RANK),
            "--dtype",
            "bfloat16",
            "--max-model-len",
            "2048",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
    )


# ── Local entrypoints ────────────────────────────────────────────────────


@app.local_entrypoint()
def train_main(
    iters: int = 400,
    batch_size: int = 32,
    lr: float = 2e-5,
    grad_accum: int = 1,
):
    train_path = Path("data/linus/sft_train.jsonl")
    val_path = Path("data/linus/sft_val.jsonl")

    if not train_path.exists():
        print(f"Dataset not found at {train_path}. Run dataset builder first.")
        return

    train_jsonl = train_path.read_text()
    val_jsonl = val_path.read_text()

    train_kb, val_kb = len(train_jsonl) // 1024, len(val_jsonl) // 1024
    print(f"Uploading dataset: train={train_kb}KB, val={val_kb}KB")

    result = train.remote(
        train_jsonl=train_jsonl,
        val_jsonl=val_jsonl,
        iters=iters,
        batch_size=batch_size,
        lr=lr,
        grad_accum=grad_accum,
    )
    print(json.dumps(result, indent=2))


@app.local_entrypoint(name="eval")
def eval_main(max_examples: int = 200):
    val_path = Path("data/linus/sft_val.jsonl")
    if not val_path.exists():
        print(f"Val set not found at {val_path}. Run dataset builder first.")
        return

    val_jsonl = val_path.read_text()
    n_examples = len([line for line in val_jsonl.strip().split("\n") if line.strip()])
    print(f"Uploading val set: {n_examples} examples ({len(val_jsonl) // 1024}KB)")

    result = evaluate.remote(val_jsonl=val_jsonl, max_examples=max_examples)
    print(json.dumps(result, indent=2))
