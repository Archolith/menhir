# Menhir Benchmark Results

Recorded benchmark reports with exact `menhir` and `archolith-bench` commit provenance. See [`../evaluation.md`](../evaluation.md) for Menhir's evaluation and publication rules.

## Recorded Runs

| Date | Report | Suite / Corpus | `menhir` Commit | `archolith-bench` Commit | Status | Headline Result |
|---|---|---|---|---|---|---|
| 2026-11-27 | [`2026-11-27-engineering-evidence-s4-s5-sprint.md`](2026-11-27-engineering-evidence-s4-s5-sprint.md) | `S4 + S5` Engineering Evidence Graph (5-pillar zero-LLM + live 5-session sprint on `simonw/llm @ 764dc386`) | `331f11b8` (`MENHIR_LOOPBACK_PROFILE=lean`) | `8a5402a` | Pilot (`n=1` live) + Deterministic CI | Deterministic: `1.0000` vs `0.2233` (`+0.7767`). Live 5-session sprint (`lean` 10-tool vs `no_menhir`): `0.8158` vs `0.5986` acc (`+21.7 pp`), `-59.4%` tokens, `-39.7%` turns, `-39.9%` tools |
| 2026-11-27 | [Prior-Art Addendum (`landscape-matrix-2026-11.md#7`)](../research/prior-art/landscape-matrix-2026-11.md) & [Tracker `#469`](https://github.com/Archolith/menhir/issues/469#issuecomment-7219756798) | 30-item stratified `LongMemEval-S` pilot (`1,497` sessions, `15,018` turns) | `d89a0d48` (base) / `48a6f551` (`#1057` / [PR `#1061`](https://github.com/Archolith/menhir/pull/1061)) | `f4b8ac2` | Preserved pilot baseline (ADR 0011) | `24/30` (`80.0%`) base (parity with `o500` Oracle across `22.5×` haystack); `26/30` (`86.7%`) across `3/3` runs with `#1057` (`0` regressions) |
| 2026-07-07 | [Tracker `#123`](https://github.com/Archolith/menhir/issues/123) (Gate E) | 500-item official `LongMemEval` Oracle | `44933985` | release-gate | Release gate (`v0.3.0`) | See tracker `#123` |
