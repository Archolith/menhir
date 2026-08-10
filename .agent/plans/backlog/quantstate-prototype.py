"""QuantState on the home turf — agent/code memory. LLM perception -> deterministic fold.

Seed corpus = this session's real work log. The load-bearing test: does the LLM CANONICALIZE
semantically-equivalent outcomes into the SAME counter (crisp in the code domain), producing
actionable experience counters like read_time_intervention_failed = N?
"""
import json, os, re, collections
import httpx

KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("QS_MODEL", "gpt-4o-mini")

# Real episodes from this session (terse agent-log phrasing; all true).
EPISODES = [
 "Prototyped fact-edge retrieval (edge-as-pointer) into recall. A/B on LongMemEval: neutral to slightly negative vs the node-only baseline.",
 "Ran a full oracle-stack A/B. Result 0.333 average, below node-only 0.400. Oracle reranking is net-negative.",
 "Isolated the TemporalOracle (semantic+temporal only) to see if it earns its place alone. Scored 0.367 vs node-only 0.400. It does not.",
 "Built BriefBuilder with the Timeline leading the brief. Measured -0.10 vs baseline: it buried the answer under date-sorted noise.",
 "Rebuilt BriefBuilder to append the Timeline BELOW the relevance list. Measured +0.03, within noise. Neutral.",
 "Built a deterministic regex detector for stated quantitative totals. Recall 5% (2 of 39). Failed.",
 "Built an LLM quant-state probe for LongMemEval object counting. Folded count matched gold 1 of 12 (8%). Failed.",
 "Fixed the SESSION-scope bug so build_context stops returning empty briefs. Verified recall went 0 -> 10. Worked.",
 "Backfilled episode and edge dates from the real session dates. Verified 1 -> 152 distinct days. Worked.",
 "Installed tiktoken to stop build_context halving the token budget. Worked.",
 "Built the retrieval-entropy instrument (D0). Validated 89 of 90, ranks difficulty deterministically. Worked.",
 "Tried the isolated TemporalOracle idea because it ranked one answer #1 earlier; at N it did not generalize. Another read-time ranking idea that did not beat node-only.",
]

SYS = (
 "You maintain a coding agent's EXPERIENTIAL COUNTERS from its work log. Read the log lines and "
 "emit counter events for recurring, actionable agent/code events: failed attempts, neutral/negative "
 "measured results, repeated errors, retries, regressions, and repeated SUCCESSFUL fixes. "
 "Each event: {\"counter\": <stable snake_case key>, \"op\": \"delta\"|\"set\", \"value\": <number, "
 "usually +1>, \"subject\": <short what-it-was>, \"evidence\": <short quote>}. "
 "CRITICAL: canonicalize semantically-equivalent outcomes to the SAME counter key. E.g. every "
 "read-time ranking/reranking/brief experiment that measured neutral-or-negative vs baseline must "
 "share ONE counter like 'read_time_intervention_failed' (do NOT invent a new key per experiment). "
 "Group correctness bug-fixes that worked under one key like 'correctness_fix_succeeded'. "
 "Output ONLY the JSON array.")


def extract(log):
    r = httpx.post("https://api.openai.com/v1/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": MODEL, "temperature": 0,
                         "messages": [{"role": "system", "content": SYS},
                                      {"role": "user", "content": log}]},
                   timeout=120)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    txt = re.sub(r'^```(json)?|```$', '', txt.strip(), flags=re.M).strip()
    return json.loads(txt)


def fold(events):
    state = collections.defaultdict(float)
    prov = collections.defaultdict(list)
    for e in events:
        if not isinstance(e, dict): continue
        c = str(e.get("counter", "")).lower().strip()
        try: v = float(e.get("value"))
        except (TypeError, ValueError): continue
        if not c: continue
        if e.get("op") == "set": state[c] = v
        else: state[c] += v
        prov[c].append(e.get("subject") or e.get("evidence") or "")
    return state, prov


def main():
    log = "\n".join(f"- {e}" for e in EPISODES)
    events = extract(log)
    state, prov = fold(events)
    print(f"QUANTSTATE on this session's work log ({MODEL}) — {len(EPISODES)} episodes -> {len(events)} events\n")
    print("EXPERIENCE COUNTERS (folded, provenance-backed):")
    for c, v in sorted(state.items(), key=lambda kv: -kv[1]):
        print(f"  {c:34s} = {int(v) if v==int(v) else v:>3}   [{', '.join(prov[c][:4])[:70]}]")
    top = max(state.items(), key=lambda kv: kv[1]) if state else None
    if top:
        print(f"\nThe experience this encodes:")
        print(f'  "{top[0]}" has fired {int(top[1])}x -> the agent should STOP trying that class of thing.')


main()
