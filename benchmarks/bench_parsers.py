"""Benchmark Python vs Rust parser implementations.

Generates synthetic test data and times each parser over multiple iterations.
Run: python benchmarks/bench_parsers.py
"""

import json
import os
import random
import string
import tempfile
import time

from snoopy._python_parsers import (
    extract_attributed_body_text as py_extract_blob,
    parse_lsof_output as py_parse_lsof,
    parse_transcript as py_parse_transcript,
)
from snoopy._native import (
    extract_attributed_body_text as rs_extract_blob,
    parse_lsof_output as rs_parse_lsof,
    parse_transcript as rs_parse_transcript,
)
from pathlib import Path


def random_text(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + " ", k=length))


def make_nsarchiver_blob(text: str) -> bytes:
    """Build a minimal NSArchiver-style blob with embedded text."""
    prefix = os.urandom(random.randint(10, 50))
    text_bytes = text.encode("utf-8")
    return prefix + b"NSString\x01\x94\x84\x01+" + bytes([len(text_bytes)]) + text_bytes + os.urandom(20)


def make_lsof_output(n_connections: int) -> str:
    header = "COMMAND   PID USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\n"
    lines = [header]
    processes = ["Chrome", "Spotify", "Slack", "Safari", "curl", "node", "Python", "ssh"]
    for i in range(n_connections):
        proc = random.choice(processes)
        pid = random.randint(1000, 65000)
        local_port = random.randint(49000, 65535)
        remote_ip = f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,255)}"
        remote_port = random.choice([80, 443, 8080, 3000, 5432, 6379])
        lines.append(
            f"{proc}  {pid} user   {i}u  IPv4 0xabc  0t0  TCP "
            f"192.168.1.5:{local_port}->{remote_ip}:{remote_port} (ESTABLISHED)\n"
        )
        # Sprinkle in some LISTEN lines that should be ignored
        if i % 5 == 0:
            lines.append(
                f"httpd  {pid+1} root   4u  IPv4 0xdef  0t0  TCP *:80 (LISTEN)\n"
            )
    return "".join(lines)


def make_transcript_file(n_entries: int) -> str:
    """Create a temp JSONL file with mixed transcript entries. Returns path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w") as f:
        for i in range(n_entries):
            ts = f"2026-02-25T10:{i // 60:02d}:{i % 60:02d}.000Z"
            kind = random.choice(["user", "assistant", "progress"])
            if kind == "user":
                entry = {
                    "type": "user",
                    "timestamp": ts,
                    "message": {"role": "user", "content": random_text(100)},
                }
            elif kind == "assistant":
                blocks = [{"type": "text", "text": random_text(200)}]
                if random.random() > 0.5:
                    blocks.append({
                        "type": "tool_use",
                        "name": random.choice(["Bash", "Read", "Write", "Grep"]),
                        "input": {"command": random_text(50)},
                    })
                entry = {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {"role": "assistant", "content": blocks},
                }
            else:
                entry = {
                    "type": "progress",
                    "timestamp": ts,
                    "data": {
                        "type": "tool_result",
                        "tool_name": "Bash",
                        "output": random_text(150),
                    },
                }
            f.write(json.dumps(entry) + "\n")
    return path


def bench(label: str, func, args, iterations: int = 100) -> float:
    """Time a function over N iterations, return mean time in ms."""
    # Warmup
    for _ in range(3):
        func(*args)

    start = time.perf_counter()
    for _ in range(iterations):
        func(*args)
    elapsed = time.perf_counter() - start
    mean_ms = (elapsed / iterations) * 1000
    return mean_ms


def main():
    random.seed(42)
    iterations = 500

    print("=" * 65)
    print("Snoopy Parser Benchmark: Python vs Rust")
    print("=" * 65)

    n_blobs = 10_000
    blobs = [make_nsarchiver_blob(random_text(random.randint(5, 200))) for _ in range(n_blobs)]

    def run_py_blobs():
        for b in blobs:
            py_extract_blob(b)

    def run_rs_blobs():
        for b in blobs:
            rs_extract_blob(b)

    py_blob_ms = bench("Python blob", run_py_blobs, (), iterations)
    rs_blob_ms = bench("Rust blob", run_rs_blobs, (), iterations)

    print(f"\nextract_attributed_body_text ({n_blobs} blobs x {iterations} iters)")
    print(f"  Python: {py_blob_ms:8.3f} ms/iter")
    print(f"  Rust:   {rs_blob_ms:8.3f} ms/iter")
    print(f"  Speedup: {py_blob_ms / rs_blob_ms:.1f}x")

    n_connections = 5_000
    lsof_output = make_lsof_output(n_connections)

    py_lsof_ms = bench("Python lsof", py_parse_lsof, (lsof_output,), iterations)
    rs_lsof_ms = bench("Rust lsof", rs_parse_lsof, (lsof_output,), iterations)

    print(f"\nparse_lsof_output ({n_connections} connections x {iterations} iters)")
    print(f"  Python: {py_lsof_ms:8.3f} ms/iter")
    print(f"  Rust:   {rs_lsof_ms:8.3f} ms/iter")
    print(f"  Speedup: {py_lsof_ms / rs_lsof_ms:.1f}x")

    n_entries = 20_000
    transcript_path = make_transcript_file(n_entries)

    py_transcript_ms = bench(
        "Python transcript",
        lambda: py_parse_transcript(Path(transcript_path)),
        (),
        iterations,
    )
    rs_transcript_ms = bench(
        "Rust transcript",
        lambda: rs_parse_transcript(transcript_path),
        (),
        iterations,
    )

    print(f"\nparse_transcript ({n_entries} entries x {iterations} iters)")
    print(f"  Python: {py_transcript_ms:8.3f} ms/iter")
    print(f"  Rust:   {rs_transcript_ms:8.3f} ms/iter")
    print(f"  Speedup: {py_transcript_ms / rs_transcript_ms:.1f}x")

    os.unlink(transcript_path)

    print(f"\n{'=' * 65}")
    print("Done.")


if __name__ == "__main__":
    main()
