(function () {
  const defaultsNode = document.getElementById('recall-lab-defaults');
  if (!defaultsNode) return;

  const initialArms = JSON.parse(defaultsNode.textContent);
  const defaultTuning = structuredClone(initialArms[0].tuning);
  let arms = structuredClone(initialArms);

  const primaryFields = [
    ['enable_bm25', 'Separate BM25 lane', 'bool'],
    ['enable_content_vector', 'Content vector lane', 'bool'],
    ['content_vector_replace_name', 'Replace name vector', 'bool'],
    ['enable_facet_candidates', 'Active facet lane', 'bool'],
    ['hybrid_alpha', 'Name vector weight', 'number', { min: 0, max: 1, step: 0.05 }],
    ['content_vector_weight', 'Content vector weight', 'number', { min: 0, max: 10, step: 0.05 }],
    ['facet_weight', 'Facet weight', 'number', { min: 0, max: 10, step: 0.05 }],
    ['fusion_admission_policy', 'Fusion admission', 'select', ['attributed', 'production_fused']],
  ];
  const advancedFields = [
    ['content_vector_k', 'Content candidates', 'number', { min: 1, max: 200, step: 1 }],
    ['similarity_scale', 'Similarity scale', 'select', ['rrf', 'normalized']],
    ['enable_fact_edges', 'Fact-edge retrieval', 'bool'],
    ['fact_edge_mode', 'Fact-edge mode', 'select', ['pointer', 'standalone']],
    ['fact_edge_k', 'Fact-edge candidates', 'number', { min: 1, max: 200, step: 1 }],
    ['enable_oracle_ranking', 'Oracle ranking', 'bool'],
    ['oracle_rank_weight', 'Oracle residual weight', 'number', { min: 0, max: 2, step: 0.05 }],
    ['enable_intent_lens', 'Intent lens', 'bool'],
    ['enable_warden_gate', 'Warden gate', 'bool'],
    ['enable_diversity_gate', 'Diversity gate', 'bool'],
    ['enable_contradiction_interrupt', 'Contradiction interrupt', 'bool'],
    ['enable_belief_gate', 'Belief gate', 'bool'],
    ['enable_evidence_anchor', 'Evidence anchor', 'bool'],
    ['enable_assertion_shadow', 'Assertion shadow (trace)', 'bool'],
    ['enable_facet_shadow', 'Facet shadow (trace)', 'bool'],
  ];

  const editor = document.getElementById('arms-editor');
  const form = document.getElementById('recall-lab-form');
  const resultRoot = document.getElementById('lab-results');
  const historyRoot = document.getElementById('lab-history');
  const status = document.getElementById('lab-status');
  const runButton = document.getElementById('run-lab');

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[ch]);
  }

  function fieldHtml(armIndex, field, wide) {
    const [key, label, type, options] = field;
    const value = arms[armIndex].tuning[key];
    let control;
    if (type === 'bool') {
      control = `<input type="checkbox" data-field="${key}" ${value ? 'checked' : ''}>`;
      return `<div class="arm-field ${wide ? 'wide' : ''}"><label>${label}${control}</label></div>`;
    }
    if (type === 'select') {
      control = `<select data-field="${key}">${options.map(option =>
        `<option value="${escapeHtml(option)}" ${option === value ? 'selected' : ''}>${escapeHtml(option)}</option>`
      ).join('')}</select>`;
    } else {
      control = `<input type="number" data-field="${key}" value="${escapeHtml(value)}" min="${options.min}" max="${options.max}" step="${options.step}">`;
    }
    return `<div class="arm-field ${wide ? 'wide' : ''}"><label>${label}</label>${control}</div>`;
  }

  function renderArms() {
    editor.innerHTML = arms.map((arm, index) => `
      <article class="arm-card ${arm.enabled ? '' : 'disabled'}" data-arm-index="${index}">
        <div class="arm-head">
          <input type="checkbox" data-role="enabled" ${arm.enabled ? 'checked' : ''} title="Enable arm">
          <span class="arm-letter">${index === 0 ? 'P' : escapeHtml(String.fromCharCode(64 + index))}</span>
          <input type="text" data-role="label" maxlength="80" value="${escapeHtml(arm.label)}">
          <div class="arm-actions">
            <button type="button" class="icon-button" data-action="reset" title="Reset arm">↺</button>
            ${arms.length > 1 ? '<button type="button" class="icon-button" data-action="remove" title="Remove arm">×</button>' : ''}
          </div>
        </div>
        <div class="arm-fields">${primaryFields.map(field => fieldHtml(index, field, false)).join('')}</div>
        <details class="advanced-fields">
          <summary>Advanced controls</summary>
          <div class="advanced-grid">${advancedFields.map(field => fieldHtml(index, field, false)).join('')}</div>
        </details>
      </article>
    `).join('');
  }

  editor.addEventListener('input', event => {
    const card = event.target.closest('[data-arm-index]');
    if (!card) return;
    const arm = arms[Number(card.dataset.armIndex)];
    if (event.target.dataset.role === 'enabled') {
      arm.enabled = event.target.checked;
      card.classList.toggle('disabled', !arm.enabled);
    } else if (event.target.dataset.role === 'label') {
      arm.label = event.target.value;
    } else if (event.target.dataset.field) {
      const key = event.target.dataset.field;
      if (event.target.type === 'checkbox') arm.tuning[key] = event.target.checked;
      else if (event.target.type === 'number') arm.tuning[key] = Number(event.target.value);
      else arm.tuning[key] = event.target.value;
    }
  });

  editor.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const index = Number(button.closest('[data-arm-index]').dataset.armIndex);
    if (button.dataset.action === 'remove') arms.splice(index, 1);
    if (button.dataset.action === 'reset') {
      const preset = initialArms.find(item => item.id === arms[index].id);
      arms[index].tuning = structuredClone(preset ? preset.tuning : defaultTuning);
    }
    renderArms();
  });

  document.getElementById('add-arm').addEventListener('click', () => {
    if (arms.length >= 9) {
      status.textContent = 'Nine concurrent arms is the safety limit.';
      return;
    }
    const suffix = arms.length + 1;
    arms.push({ id: `custom-${Date.now()}`, label: `Custom ${suffix}`, enabled: true, tuning: structuredClone(defaultTuning) });
    renderArms();
  });

  function formatNumber(value, digits) {
    return value === null || value === undefined ? '—' : Number(value).toFixed(digits);
  }

  function errorMessage(payload) {
    if (typeof payload.detail === 'string') return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map(item => item.msg).join('; ');
    return 'Recall Lab request failed.';
  }

  function resultCard(memory, baselineRanks, isBaseline) {
    const baselineRank = baselineRanks.get(memory.uuid);
    const movement = baselineRank == null ? null : baselineRank - memory.rank;
    let delta = '';
    if (!isBaseline) {
      if (baselineRank == null) delta = '<span class="rank-delta up">new</span>';
      else if (movement > 0) delta = `<span class="rank-delta up">↑${movement}</span>`;
      else if (movement < 0) delta = `<span class="rank-delta down">↓${Math.abs(movement)}</span>`;
      else delta = '<span class="rank-delta">same</span>';
    }
    const sources = (memory.contributing_sources || []).length
      ? memory.contributing_sources.join(' + ')
      : (memory.source || 'unattributed');
    return `<article class="memory-result">
      <div class="memory-top">
        <span class="rank">${memory.rank}</span>
        <div class="memory-name">${escapeHtml(memory.name || memory.uuid)}</div>
        ${delta}
      </div>
      ${memory.content ? `<div class="memory-content">${escapeHtml(memory.content)}</div>` : ''}
      <div class="memory-meta">
        <span>${escapeHtml(memory.memory_type)}</span><span>${escapeHtml(memory.scope)}</span><span>${escapeHtml(sources)}</span>
        ${memory.bm25_rank != null ? `<span>bm25 #${memory.bm25_rank}</span>` : ''}
        ${memory.cosine_rank != null ? `<span>name #${memory.cosine_rank}</span>` : ''}
        ${memory.content_rank != null ? `<span>content #${memory.content_rank}</span>` : ''}
      </div>
      <div class="score-line"><span>${escapeHtml(memory.uuid)}</span><strong>${formatNumber(memory.final_score, 4)}</strong></div>
    </article>`;
  }

  function labelForArm(payload, armId) {
    return payload.arms.find(arm => arm.id === armId)?.label || armId;
  }

  function judgmentHtml(payload) {
    const judgment = payload.judgment;
    if (!judgment) return '';
    if (!judgment.ok) return `<section class="judge-card failed">
      <div class="judge-head"><h3>LLM judge</h3><span class="badge failed">${judgment.skipped ? 'SKIPPED' : 'FAILED'}</span></div>
      <p class="judge-reason">${escapeHtml(judgment.error)}</p>
    </section>`;
    const verdict = judgment.winner_id
      ? labelForArm(payload, judgment.winner_id)
      : `Tie: ${(judgment.tied_ids || []).map(id => labelForArm(payload, id)).join(' / ')}`;
    const reasons = (judgment.passes || []).map((pass, index) =>
      `<p class="judge-reason"><strong>Pass ${index + 1}:</strong> ${escapeHtml(pass.reason)}</p>`
    ).join('');
    const scores = Object.entries(judgment.average_scores || {}).map(([armId, values]) => {
      const overall = Object.values(values).reduce((sum, value) => sum + Number(value), 0) / 5;
      return `<div class="judge-score"><strong>${escapeHtml(labelForArm(payload, armId))} · ${formatNumber(overall, 2)}/5</strong>
        rel ${formatNumber(values.relevance, 1)} · complete ${formatNumber(values.completeness, 1)} · rank ${formatNumber(values.ranking, 1)} · noise ${formatNumber(values.noise_control, 1)} · omissions ${formatNumber(values.omission_safety, 1)}
      </div>`;
    }).join('');
    return `<section class="judge-card">
      <div class="judge-head"><h3>Blinded LLM verdict</h3><span class="judge-verdict">${escapeHtml(verdict)}</span></div>
      <div class="judge-meta"><span>${escapeHtml(judgment.model)}</span><span>${judgment.passes_completed}/${judgment.passes_requested} passes</span><span>${judgment.stable ? 'stable decision' : 'passes disagreed'}</span><span>${formatNumber(judgment.elapsed_ms, 0)} ms</span></div>
      <div class="judge-scores">${scores}</div>${reasons}
    </section>`;
  }

  function resultPayloadHtml(payload) {
    const successful = payload.arms.filter(arm => arm.ok);
    const baseline = successful[0];
    const baselineRanks = new Map((baseline?.results || []).map(item => [item.uuid, item.rank]));
    const columns = payload.arms.map(arm => {
      if (!arm.ok) return `<article class="result-column failed">
        <div class="result-head"><div class="result-title"><h3>${escapeHtml(arm.label)}</h3><span class="badge failed">FAILED</span></div></div>
        <div class="error-card">${escapeHtml(arm.error)}</div>
        <details class="result-details"><summary>Configuration</summary><pre>${escapeHtml(JSON.stringify(arm.tuning, null, 2))}</pre></details>
      </article>`;
      const degraded = Boolean(arm.degraded || arm.search_error);
      const overlap = arm === baseline ? arm.results.length : arm.results.filter(item => baselineRanks.has(item.uuid)).length;
      return `<article class="result-column">
        <div class="result-head">
          <div class="result-title"><h3>${escapeHtml(arm.label)}</h3><span class="badge ${degraded ? 'failed' : 'persistent'}">${degraded ? 'DEGRADED' : 'OK'}</span></div>
          <div class="result-metrics"><span>${formatNumber(arm.elapsed_ms, 1)} ms wall</span><span>${arm.candidates_evaluated} candidates</span><span>${overlap}/${baseline?.results.length || 0} baseline overlap</span></div>
        </div>
        ${degraded ? `<div class="error-card">${escapeHtml(arm.note || arm.search_error || 'Recall search backend failed; results may be incomplete.')}</div>` : ''}
        <div class="memory-list">${arm.results.map(item => resultCard(item, baselineRanks, arm === baseline)).join('') || '<div class="empty-state">No results.</div>'}</div>
        <details class="result-details"><summary>Timing phases and exact configuration</summary><pre>${escapeHtml(JSON.stringify({ phases: arm.phases, tuning: arm.tuning }, null, 2))}</pre></details>
      </article>`;
    }).join('');
    const saved = payload.saved_run_id ? `<span class="summary-chip">Saved run #${payload.saved_run_id}</span>` : '';
    const summary = payload.arms.map(arm => `<span class="summary-chip">${escapeHtml(arm.label)} · ${arm.ok ? `${arm.results.length} hits / ${formatNumber(arm.elapsed_ms, 1)} ms${arm.degraded ? ' / degraded' : ''}` : 'failed'}</span>`).join('');
    return `${judgmentHtml(payload)}<div class="result-summary">${saved}${summary}</div><div class="result-grid">${columns}</div>`;
  }

  async function loadHistory() {
    try {
      const response = await fetch('/explorer/api/recall-lab/history?limit=100');
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload));
      if (!payload.runs.length) {
        historyRoot.innerHTML = '<div class="empty-state">No saved experiments yet.</div>';
        return;
      }
      historyRoot.innerHTML = `<div class="history-list">${payload.runs.map(run => {
        const labels = new Map((run.arms || []).map(arm => [arm.id, arm.label]));
        const verdict = run.winner_id
          ? `Winner: ${labels.get(run.winner_id) || run.winner_id}`
          : (run.tied_ids || []).length
            ? `Tie: ${run.tied_ids.map(id => labels.get(id) || id).join(' / ')}`
            : (run.judge_enabled ? 'Judge failed' : 'Not judged');
        return `<button type="button" class="history-row" data-run-id="${run.id}">
          <span class="history-id">#${run.id}</span><span class="history-query">${escapeHtml(run.query)}</span>
          <span class="history-verdict">${escapeHtml(verdict)}</span><time class="history-time">${escapeHtml(new Date(run.recorded_at).toLocaleString())}</time>
        </button>`;
      }).join('')}</div>`;
    } catch (error) {
      historyRoot.innerHTML = `<div class="error-card">${escapeHtml(error.message)}</div>`;
    }
  }

  historyRoot.addEventListener('click', async event => {
    const row = event.target.closest('[data-run-id]');
    if (!row) return;
    try {
      const response = await fetch(`/explorer/api/recall-lab/history/${row.dataset.runId}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload));
      payload.result.saved = true;
      payload.result.saved_run_id = payload.id;
      renderResults([payload.result]);
    } catch (error) {
      status.textContent = error.message;
    }
  });

  document.getElementById('refresh-history').addEventListener('click', loadHistory);

  function batchScoreboardHtml(payloads) {
    if (payloads.length < 2) return '';
    const rows = new Map();
    payloads[0].arms.forEach(arm => rows.set(arm.id, { label: arm.label, wins: 0, ties: 0, score: 0, scored: 0 }));
    let judged = 0;
    payloads.forEach(payload => {
      const judgment = payload.judgment;
      if (!judgment?.ok) return;
      judged += 1;
      if (judgment.winner_id && rows.has(judgment.winner_id)) rows.get(judgment.winner_id).wins += 1;
      else (judgment.tied_ids || []).forEach(id => { if (rows.has(id)) rows.get(id).ties += 1; });
      Object.entries(judgment.average_scores || {}).forEach(([id, values]) => {
        if (!rows.has(id)) return;
        const overall = Object.values(values).reduce((sum, value) => sum + Number(value), 0) / 5;
        rows.get(id).score += overall;
        rows.get(id).scored += 1;
      });
    });
    const tableRows = [...rows.values()].sort((a, b) => b.wins - a.wins || (b.score / Math.max(1, b.scored)) - (a.score / Math.max(1, a.scored))).map(row =>
      `<tr><td>${escapeHtml(row.label)}</td><td>${row.wins}</td><td>${row.ties}</td><td>${row.scored ? formatNumber(row.score / row.scored, 2) : '—'}</td></tr>`
    ).join('');
    return `<section class="batch-scoreboard"><h2>Batch scoreboard · ${judged}/${payloads.length} judged</h2>
      <table class="batch-table"><thead><tr><th>Arm</th><th>Wins</th><th>Ties</th><th>Mean judge score /5</th></tr></thead><tbody>${tableRows}</tbody></table>
    </section>`;
  }

  function renderResults(payloads) {
    if (payloads.length === 1) {
      resultRoot.innerHTML = resultPayloadHtml(payloads[0]);
    } else {
      const queries = payloads.map((payload, index) => {
        const verdict = payload.judgment?.winner_label || (payload.judgment?.ok ? 'tie' : 'unjudged');
        return `<details class="batch-query" ${index === payloads.length - 1 ? 'open' : ''}>
          <summary>${index + 1}. ${escapeHtml(payload.query)} · ${escapeHtml(verdict)}</summary>
          <div class="batch-query-body">${resultPayloadHtml(payload)}</div>
        </details>`;
      }).join('');
      resultRoot.innerHTML = `${batchScoreboardHtml(payloads)}${queries}`;
    }
    resultRoot.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const queries = document.getElementById('lab-query').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!queries.length) return;
    if (queries.length > 50) {
      status.textContent = 'A batch can contain at most 50 queries.';
      return;
    }
    if (queries.some(query => query.length > 1000)) {
      status.textContent = 'Each query must be 1,000 characters or fewer.';
      return;
    }
    const sharedBody = {
      preset: document.getElementById('lab-preset').value,
      namespace: document.getElementById('lab-namespace').value.trim() || null,
      limit: Number(document.getElementById('lab-limit').value),
      candidate_k: Number(document.getElementById('lab-candidate-k').value),
      include_session: document.getElementById('lab-include-session').checked,
      include_superseded: document.getElementById('lab-include-superseded').checked,
      include_invalidated: document.getElementById('lab-include-invalidated').checked,
      judge: document.getElementById('lab-judge').checked,
      judge_passes: Number(document.getElementById('lab-judge-passes').value),
      arms
    };
    runButton.disabled = true;
    const payloads = [];
    try {
      for (let index = 0; index < queries.length; index += 1) {
        status.textContent = `Running query ${index + 1}/${queries.length}${sharedBody.judge ? ' and asking the blinded judge' : ''}…`;
        const response = await fetch('/explorer/api/recall-lab/run', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...sharedBody, query: queries[index] })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(`Query ${index + 1}: ${errorMessage(payload)}`);
        payloads.push(payload);
        renderResults(payloads);
      }
      const failedJudges = payloads.filter(payload => payload.judgment && !payload.judgment.ok).length;
      await loadHistory();
      status.textContent = `Finished ${payloads.length} quer${payloads.length === 1 ? 'y' : 'ies'}${failedJudges ? ` with ${failedJudges} judge failure${failedJudges === 1 ? '' : 's'}` : ''}.`;
    } catch (error) {
      status.textContent = error.message;
      resultRoot.innerHTML = `<div class="error-card">${escapeHtml(error.message)}</div>`;
    } finally {
      runButton.disabled = false;
    }
  });

  renderArms();
  loadHistory();
})();
