# Catching a GPU that lies about its model: bit-exact verification of LLM inference

*November 2026. Code, contract and raw measurements:
[github.com/marselzaripov1988/verifiable-inference](https://github.com/marselzaripov1988/verifiable-inference).*

## The problem

You pay a stranger with a GPU to run a language model. They have every
reason to cheat: a smaller model, heavier quantization or a sampled
answer is cheaper to produce and still reads plausibly. The usual
answers in decentralized compute are to run every job twice (and pay
twice), or not to check at all.

This project is a working protocol that checks without redoing the
work. It runs on a public Cosmos chain: a CosmWasm contract holds the
executors' stake, and Qwen2.5‑7B and Qwen3‑8B on RTX 3090 and 4090 serve
real requests.

## Verifying is 41× cheaper than generating

A verifier does not need to regenerate the answer. It takes the
finished text and asks the model what probability it assigns to each of
those tokens: one forward pass instead of hundreds of sequential ones.
Measured, that is **41× cheaper** than generation, so four independent
verifiers cost **9.7%** of the job rather than 400%.

The hard question is the next one: how far apart are the probabilities
of two *honest* nodes, and can that be told apart from fraud?

## Statistics were not enough; bit-exactness was

The first design compared probability vectors against a threshold.
Across 1,520 honest observations the mean divergence was 0.0127, but the
worst honest case was 0.0447, while the least visible fraud measured
(FP8 weights) was 0.0378. Honest and fraudulent overlapped. A
conjunctive rule over four verifiers made it workable, but only barely.

Measuring further showed why. On the **same card**, with the same model,
vLLM version and dtype, switching `--enforce-eager` gave **0 of 40**
bit-identical answers, with a mean divergence of 49,424 millionths and a
maximum of 413,488. That is squarely inside the fraud band. An honest node
in the wrong mode looks like a cheater, and every name it declares is
correct.

So the design moved from "close enough" to "identical to the bit",
which required finding out what actually determines the bits.

**The card model, not the architecture.** A kernel cache built on an RTX
4090 was rejected by an RTX 4500 Ada and an L40S (same Ada architecture).
Both recompiled, and neither produced a single bit-identical match out
of 334 texts.

**Kernel selection, not the driver.** Of four RTX 3090s, three matched
bit for bit across drivers 580, 595 and 615. The fourth matched none.
It differed in two of eight autotuned kernel configs, one of them a
reduction block size (`R0_BLOCK` 2048 vs 4096), which is literally the
order of summation. Copying the matching card's kernel cache onto it
gave **300 of 300** identical outputs: same card, same driver, only the
cache replaced.

**Autotuning is noise.** Without pinning compile flags (no FlashInfer
autotune, no combo kernels, no prefix caching), two kernel builds on the
same card agree on **0 of 200** texts. With them pinned: **200 of 200**.

**Batching changes the bits.** Scoring 2, 4 or 8 requests concurrently
kept 16, 4 and 2 of 16 results identical. Server load is part of the
problem too, and it is not yet fully solved here.

The result is a *class*: card model, model weights, runtime, dtype,
launch flags and the **hash of a pinned kernel cache**, all recorded on
chain. Inside a class, honest divergence is exactly zero. For
Qwen3‑8B on the RTX 3090, one 2.9 MB cache reproduced bit for bit on
six machines with drivers 580.126 through 615.71. A fresh kernel build
on one of them diverged by up to 624,860 millionths.

## The verdict: two stages, no false accusations

```
verifier's cache ≠ cache on chain       → no verdict (incomparable)
cache matches, hash matches             → honest
cache matches, hash differs             → guilty, stake slashed
```

The first stage makes a false accusation impossible by construction,
not by a safety margin. A node with a mismatched kernel cache, for
example after a runtime patch, is excluded rather than slashed.

Admission is a **zero‑tolerance probe**. Before taking jobs, a node
scores 34 reference texts and must reproduce the reference vector
exactly. Measured:

| Node | Probe texts matched | Worst divergence (millionths) |
|---|---|---|
| honest card, other machine and driver | 34/34 | 0 |
| same card, kernel cache transplanted | 34/34 | 0 |
| wrong mode (eager) | 0/34 | 28,025 |
| FP8 fraud | 0/34 | 118,136 |

A node can lie about its cache hash, but the probe checks behaviour, so
the lie fails the first probe.

## Catching sampled or substituted answers

Bit-exact scoring proves the executor reported the true probabilities
of *its* text. It does not prove the text came from greedy decoding of
the pinned model. A second check looks at every token of the answer:
how far its log-probability is below the best token at that position.
Honest greedy output should sit at zero.

The first run found 4% of honest tokens were not the top choice. The
cause was a hidden setting: vLLM applies the model's
`generation_config.json`, and Qwen2.5 ships `repetition_penalty: 1.05`.
The nodes now disable it explicitly. After the fix, the rule "at least
3 tokens with a gap above 0.5" (5 tokens for Qwen3) gave:

| Answers (32 per row) | Flagged |
|---|---|
| honest, Qwen2.5‑7B (96 answers) / Qwen3‑8B (128) | **0** / **0** |
| sampled at T = 0.7 / 1.0 | 29 / 28 |
| Qwen2.5‑1.5B / 3B substituted | 31 / 30 |
| same 7B, AWQ 4‑bit | 27 |
| same 7B, FP8 | 1 |

**Limitation, stated plainly:** FP8 of the same model is not caught at a
safe threshold. A lower threshold catches 21 of 32 but starts to touch
honest answers. FP8 buys the cheater about 1.4× throughput. For now the
check is logged, not enforced on chain.

## What is running

- **Contract** (Rust, CosmWasm): one registry per class, stake and
  slashing, a zero‑tolerance probe, configuration that can be upgraded
  without re-registering nodes.
- **Node image**: one Docker image for every class. It reads the model
  and class from the chain and picks the matching kernel cache and
  reference probe. It refuses to run on an unknown card or on drivers
  below 580, which fail with CUDA Error 804. CI verifies every cache
  and probe file by hash.
- **Operations**: a scheduler rents and releases RTX 4090s on vast.ai for
  the network's evening hours. Keys reach a rented machine only through
  ssh stdin and are wiped at shutdown. A Telegram bot serves end users
  and accepts payment.
- **Measurement discipline**: every claim in the repository is backed by
  saved vectors, and the tests recompute the published numbers from
  them. A typical experiment costs $0.05–0.30 of GPU rental.

## What this shows

Verifying LLM inference is mostly not cryptography. It is an engineering
problem of making floating-point computation reproducible, and of
measuring, rather than assuming, where reproducibility breaks: card
model, kernel autotuning, batching, a default in a config file nobody
reads. Once it is reproducible, verification is a hash comparison, and
honest nodes cannot be accused.
