(function () {
  const form = document.getElementById('extraction-lab-form');
  if (!form) return;

  const variants = [
    'baseline', 'minus_when_in_doubt', 'minimal_recall_patch', 'mention_first',
    'update_aware', 'proposition_first', 'mention_first_update_aware',
    'proposition_first_structured', 'combined_extraction'
  ];
  const episodes = [];
  const arms = [newArm('baseline', 'Baseline', 'baseline')];
  const episodeEditor = document.getElementById('episodes-editor');
  const armEditor = document.getElementById('arms-editor');
  const results = document.getElementById('lab-results');
  const status = document.getElementById('lab-status');
  const runButton = document.getElementById('run-lab');

  function newArm(id, label, promptVariant) {
    return {
      id, label, enabled: true,
      tuning: {
        prompt_variant: promptVariant,
        model: null,
        context_episode_count: 10,
        temperature: 0,
        enable_candidate_lookup: false,
        forced_known_entities: null,
        retrieved_context: null
      }
    };
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[char]);
  }

  function lines(id) {
    return document.getElementById(id).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  }

  function renderEpisodes() {
    if (!episodes.length) {
      episodeEditor.innerHTML = '<div class="empty-state">No previous episodes.</div>';
      return;
    }
    episodeEditor.innerHTML = episodes.map((episode, index) => `
      <article class="arm-card" data-episode-index="${index}">
        <div class="arm-head"><strong>Episode ${index + 1}</strong>
          <button type="button" class="icon-button" data-action="remove-episode" title="Remove episode">×</button>
        </div>
        <textarea rows="3" maxlength="8000" data-role="episode" required>${escapeHtml(episode.text)}</textarea>
      </article>`).join('');
  }

  function renderArms() {
    armEditor.innerHTML = arms.map((arm, index) => `
      <article class="arm-card ${arm.enabled ? '' : 'disabled'}" data-arm-index="${index}">
        <div class="arm-head">
          <input type="checkbox" data-role="enabled" ${arm.enabled ? 'checked' : ''} title="Enable arm">
          <input type="text" data-role="label" maxlength="80" value="${escapeHtml(arm.label)}" required>
          ${arms.length > 1 ? '<button type="button" class="icon-button" data-action="remove-arm" title="Remove arm">×</button>' : ''}
        </div>
        <div class="arm-fields">
          <label>Prompt variant<select data-field="prompt_variant">${variants.map(value =>
            `<option value="${value}" ${value === arm.tuning.prompt_variant ? 'selected' : ''}>${value}</option>`
          ).join('')}</select></label>
          <label>Model <span class="label-hint">blank uses production default</span>
            <input type="text" data-field="model" value="${escapeHtml(arm.tuning.model || '')}"></label>
          <label>Context episodes<input type="number" data-field="context_episode_count" min="1" max="100" value="${arm.tuning.context_episode_count}"></label>
          <label>Temperature<input type="number" data-field="temperature" min="0" max="2" step="0.1" value="${arm.tuning.temperature}"></label>
          <label>Candidate lookup<input type="checkbox" data-field="enable_candidate_lookup" ${arm.tuning.enable_candidate_lookup ? 'checked' : ''}></label>
        </div>
      </article>`).join('');
  }

  episodeEditor.addEventListener('input', event => {
    const card = event.target.closest('[data-episode-index]');
    if (card && event.target.dataset.role === 'episode') episodes[Number(card.dataset.episodeIndex)].text = event.target.value;
  });
  episodeEditor.addEventListener('click', event => {
    const button = event.target.closest('[data-action="remove-episode"]');
    if (!button) return;
    episodes.splice(Number(button.closest('[data-episode-index]').dataset.episodeIndex), 1);
    renderEpisodes();
  });

  armEditor.addEventListener('input', event => {
    const card = event.target.closest('[data-arm-index]');
    if (!card) return;
    const arm = arms[Number(card.dataset.armIndex)];
    if (event.target.dataset.role === 'enabled') arm.enabled = event.target.checked;
    if (event.target.dataset.role === 'label') arm.label = event.target.value;
    if (event.target.dataset.field) {
      const key = event.target.dataset.field;
      if (event.target.type === 'checkbox') arm.tuning[key] = event.target.checked;
      else if (event.target.type === 'number') arm.tuning[key] = Number(event.target.value);
      else arm.tuning[key] = event.target.value.trim() || null;
    }
    card.classList.toggle('disabled', !arm.enabled);
  });
  armEditor.addEventListener('click', event => {
    const button = event.target.closest('[data-action="remove-arm"]');
    if (!button) return;
    arms.splice(Number(button.closest('[data-arm-index]').dataset.armIndex), 1);
    renderArms();
  });

  document.getElementById('add-episode').addEventListener('click', () => {
    episodes.push({ text: '', created_at: new Date().toISOString() });
    renderEpisodes();
    episodeEditor.querySelector('[data-episode-index]:last-child textarea').focus();
  });
  document.getElementById('add-arm').addEventListener('click', () => {
    if (arms.length >= 16) return void (status.textContent = 'Sixteen arms is the request limit.');
    const suffix = arms.length + 1;
    arms.push(newArm(`arm-${Date.now()}`, `Variant ${suffix}`, 'update_aware'));
    renderArms();
  });

  function score(value) {
    return value === null || value === undefined ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
  }

  function renderResults(payload) {
    results.innerHTML = payload.arms.map(arm => {
      if (!arm.ok) return `<article class="arm-card failed"><h3>${escapeHtml(arm.label)}</h3><p>${escapeHtml(arm.error || 'Extraction failed')}</p></article>`;
      const extraction = arm.extraction || { mentions: [], propositions: [] };
      const scores = arm.gold_scores || {};
      return `<article class="arm-card">
        <div class="arm-head"><h3>${escapeHtml(arm.label)}</h3><span>${Number(arm.elapsed_ms).toFixed(0)} ms</span></div>
        <div class="score-line"><span>Mention recall / precision</span><strong>${score(scores.mention_recall)} / ${score(scores.mention_precision)}</strong></div>
        <div class="score-line"><span>Proposition recall / precision</span><strong>${score(scores.proposition_recall)} / ${score(scores.proposition_precision)}</strong></div>
        <div class="score-line"><span>Update capture</span><strong>${score(scores.update_capture_rate)}</strong></div>
        <h4>Mentions (${extraction.mentions.length})</h4><pre>${escapeHtml(JSON.stringify(extraction.mentions, null, 2))}</pre>
        <h4>Propositions (${extraction.propositions.length})</h4><pre>${escapeHtml(JSON.stringify(extraction.propositions, null, 2))}</pre>
      </article>`;
    }).join('');
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    runButton.disabled = true;
    status.textContent = 'Running extraction arms…';
    const payload = {
      current_message: document.getElementById('lab-message').value,
      previous_episodes: episodes.map(episode => ({ ...episode, text: episode.text.trim() })).filter(episode => episode.text),
      gold: {
        mentions: lines('lab-gold-mentions'),
        propositions: lines('lab-gold-propositions'),
        update_language: lines('lab-gold-updates')
      },
      arms
    };
    try {
      const response = await fetch('/explorer/api/extraction-lab/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Extraction request failed.');
      renderResults(body);
      status.textContent = body.saved ? `Complete. Saved as run ${body.saved_run_id}.` : 'Complete.';
    } catch (error) {
      status.textContent = error.message;
      results.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    } finally {
      runButton.disabled = false;
    }
  });

  renderEpisodes();
  renderArms();
}());
