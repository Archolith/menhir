(function () {
  let cy = null;
  let currentSelection = null;

  function byId(id) { return document.getElementById(id); }

  // Tab Switching
  document.addEventListener('click', e => {
    const tabBtn = e.target.closest('.tab-btn');
    if (tabBtn) {
        const tabId = tabBtn.dataset.tab;
        document.querySelectorAll('.sidebar-content').forEach(el => el.style.display = 'none');
        document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
        const target = byId('sidebar-' + tabId);
        if (target) target.style.display = 'block';
        tabBtn.classList.add('active');
    }
  });

  // Polling
  async function pollSystem() {
    const opsTab = byId('sidebar-system');
    if (opsTab && opsTab.style.display !== 'none') {
        await fetchHtml('/explorer/partials/queue', 'queue-panel');
        await fetchHtml('/explorer/partials/successful', 'successful-panel');
        await fetchHtml('/explorer/partials/recovered', 'recovered-panel');
        await fetchHtml('/explorer/partials/pending_actions', 'pending-actions-panel');
        await fetchHtml('/explorer/partials/failed', 'failed-panel');
        await fetchHtml('/explorer/partials/mcp_events', 'mcp-events-panel');
    }
  }
  setInterval(pollSystem, 5000);

  // Graph Init
  function initGraph() {
    const container = byId('cy');
    if (!container) return;

    cy = cytoscape({
      container: container,
      style: [
        {
          selector: 'node',
          style: {
            'label': 'data(label)',
            'color': '#f8fafc',
            'font-size': '10px',
            'text-valign': 'bottom',
            'text-margin-y': '6px',
            'background-color': '#2563eb',
            'width': '30px',
            'height': '30px',
            'border-width': 2,
            'border-color': '#1e293b'
          }
        },
        {
          selector: 'node.episode',
          style: {
            'shape': 'round-rectangle',
            'background-color': '#f97316',
            'width': '45px',
            'height': '25px'
          }
        },
        {
          selector: 'node.persistent',
          style: { 'border-color': '#10b981', 'border-width': 4 }
        },
        {
          selector: 'node.promoted',
          style: { 'border-color': '#8b5cf6', 'border-width': 4 }
        },
        {
          selector: 'edge',
          style: {
            'width': 2,
            'line-color': '#334155',
            'target-arrow-color': '#334155',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'label': 'data(label)',
            'font-size': '8px',
            'color': '#94a3b8'
          }
        }
      ]
    });
    
    cy.on('tap', 'node', function(evt) {
      loadNode(evt.target.id(), null);
    });
  }

  async function fetchHtml(url, targetId) {
    const target = byId(targetId);
    if (!target) return;
    try {
      const response = await fetch(url);
      if (response.ok) {
        target.innerHTML = await response.text();
        applyLocalTime();
      }
    } catch (e) { }
  }

  // READABLE TIMESTAMPS
  function formatTime(isoStr) {
    if (!isoStr) return "";
    const date = new Date(isoStr.replace(' ', 'T'));
    const now = new Date();
    const diffInSeconds = Math.floor((now - date) / 1000);
    
    if (isNaN(date.getTime())) return isoStr;

    if (diffInSeconds < 60) return 'just now';
    if (diffInSeconds < 3600) return Math.floor(diffInSeconds / 60) + 'm ago';
    
    const isToday = date.toDateString() === now.toDateString();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const isYesterday = date.toDateString() === yesterday.toDateString();
    
    const timeStr = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    
    if (isToday) return `Today at ${timeStr}`;
    if (isYesterday) return `Yesterday at ${timeStr}`;
    
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ` at ${timeStr}`;
  }

  function applyLocalTime() {
    document.querySelectorAll('.utc-time').forEach(el => {
      const isoStr = el.dataset.utc;
      if (isoStr) {
        el.textContent = formatTime(isoStr);
        el.title = new Date(isoStr.replace(' ', 'T')).toLocaleString();
        el.classList.remove('utc-time');
        el.classList.add('local-time-ready');
      }
    });
  }

  async function loadNode(uuid, trigger) {
    currentSelection = { kind: 'node', id: uuid };
    if (trigger) setActiveCard(trigger);
    
    fetchHtml('/explorer/partials/node/' + uuid, 'detail-panel');
    const depth = byId('graph-depth') ? byId('graph-depth').value : 1;
    
    try {
        const gResp = await fetch('/explorer/api/graph/' + uuid + '?depth=' + depth);
        if (gResp.ok) {
            const data = await gResp.json();
            cy.json({ elements: data.elements });
            cy.resize();
            cy.layout({ 
                name: 'cose', 
                animate: true, 
                fit: true, 
                padding: 100,
                componentSpacing: 150,
                nodeRepulsion: 8000,
                idealEdgeLength: 100,
                edgeElasticity: 100
            }).run();
        }
    } catch (e) { }
  }

  function setActiveCard(el) {
    document.querySelectorAll('.row-card').forEach(c => c.classList.remove('active'));
    const card = el.closest('.row-card');
    if (card) card.classList.add('active');
  }

  document.addEventListener('click', e => {
    const nodeTrigger = e.target.closest('[data-node-uuid]');
    if (nodeTrigger) {
        e.preventDefault();
        loadNode(nodeTrigger.dataset.nodeUuid, nodeTrigger);
    }
  });

  // Candidate Approval/Rejection
  document.addEventListener('click', e => {
    const approveBtn = e.target.closest('.btn-approve');
    if (approveBtn) {
        e.preventDefault();
        const uuid = approveBtn.dataset.uuid;
        fetch('/explorer/candidates/' + uuid + '/approve', { method: 'POST' })
            .then(resp => {
                if (resp.ok) {
                    fetchHtml('/explorer/partials/candidates', 'candidates-panel');
                }
            })
            .catch(err => { });
        return;
    }

    const rejectBtn = e.target.closest('.btn-reject');
    if (rejectBtn) {
        e.preventDefault();
        const uuid = rejectBtn.dataset.uuid;
        fetch('/explorer/candidates/' + uuid + '/reject', { method: 'POST' })
            .then(resp => {
                if (resp.ok) {
                    fetchHtml('/explorer/partials/candidates', 'candidates-panel');
                }
            })
            .catch(err => { });
        return;
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    initGraph();
    applyLocalTime();
    setTimeout(() => {
        const initial = document.body.dataset.initialEpisodeUuid;
        if (initial) {
            const trigger = document.querySelector('[data-node-uuid=\"' + initial + '\"]');
            loadNode(initial, trigger);
        }
    }, 200);
  });
})();
