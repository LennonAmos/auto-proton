(() => {
  'use strict';
  const key = '__autoProtonUI';
  if (globalThis[key]) globalThis[key].cleanup();
  const records = new Map(), pending = new Map();
  let events = [], heartbeat = Date.now(), watchdog;
  const style = `
    [data-auto-proton-panel] { margin: 0 0 22px; padding: 18px 20px; border: 1px solid #44536a;
      border-radius: 6px; background: #202c3c; color: #e5ecf5; font: 14px/1.5 sans-serif; }
    [data-auto-proton-panel] label { display:flex; align-items:center; gap:10px; cursor:pointer; font-weight:600; }
    [data-auto-proton-panel] input { width:17px; height:17px; accent-color:#66c0f4; cursor:pointer; }
    [data-auto-proton-panel] output { display:block; margin-top:12px; color:#9ed8ff; }
    [data-auto-proton-panel] p { margin:5px 0 0; color:#adbaca; font-size:12px; }
    [data-auto-proton-managed] { opacity: .55; }
  `;
  function release(record) {
    record.body.inert = record.inert;
    record.body.removeAttribute('data-auto-proton-managed');
    record.panel.remove();
    record.css.remove();
  }
  function cleanup() {
    clearInterval(watchdog);
    for (const record of records.values()) release(record);
    records.clear(); pending.clear(); events = [];
    if (globalThis[key] === api) delete globalThis[key];
  }
  function tick(payload) {
    heartbeat = Date.now();
    const seen = new Set();
    for (const popup of g_PopupManager.GetPopups()) {
      // Steam's internal app-properties document; never touch store/community/browser content.
      let doc;
      try { doc = popup.window.document; } catch (_) { continue; }
      const pane = doc.querySelector('[role="tabpanel"][id*="/properties/compatibility_Content"]');
      if (!pane) continue;
      const match = pane.id.match(/\/app\/(\d+)\/properties\/compatibility_Content$/);
      if (!match || !payload.games[match[1]]) continue;
      const appid = match[1], game = payload.games[appid];
      const body = pane.querySelector('.DialogBody');
      if (!body) continue;
      seen.add(pane);
      let record = records.get(pane);
      if (!record || !record.panel.isConnected) {
        if (record) release(record);
        const panel = doc.createElement('section');
        panel.dataset.autoProtonPanel = appid;
        panel.setAttribute('aria-label', 'Auto-Proton');
        const label = doc.createElement('label');
        const checkbox = doc.createElement('input'); checkbox.type = 'checkbox';
        checkbox.setAttribute('aria-label', 'Override Auto-Proton');
        label.append(checkbox, doc.createTextNode('Override Auto-Proton'));
        const output = doc.createElement('output'); output.setAttribute('aria-live','polite');
        const detail = doc.createElement('p');
        panel.append(label, output, detail);
        const css = doc.createElement('style'); css.textContent = style; doc.head.append(css);
        body.before(panel);
        record = {panel, css, checkbox, output, detail, body, inert:body.inert};
        checkbox.onchange = () => {
          const enabled = checkbox.checked;
          pending.set(appid, enabled);
          events.push({appid, enabled});
          body.inert = !enabled;
          body.toggleAttribute('data-auto-proton-managed', !enabled);
          detail.textContent = enabled ? 'Choose a version using Steam’s selector below. This choice is saved for this game.' : 'Automatic selection is active. Checking the current recommendation…';
        };
        records.set(pane, record);
      }
      if (pending.has(appid) && pending.get(appid) === game.manual) pending.delete(appid);
      const manual = pending.has(appid) ? pending.get(appid) : game.manual;
      record.checkbox.checked = manual;
      record.body.inert = !manual;
      record.body.toggleAttribute('data-auto-proton-managed', !manual);
      record.output.textContent = 'Automatic choice: ' + game.choice;
      record.detail.textContent = manual ? 'Choose a version using Steam’s selector below. This choice is saved for this game.' : game.detail;
    }
    for (const [pane, record] of records) {
      if (!seen.has(pane)) { release(record); records.delete(pane); }
    }
    const result = events; events = []; return result;
  }
  const api = {tick, cleanup}; globalThis[key] = api;
  // Restore Steam controls if the background process disappears, even after a crash or SIGKILL.
  watchdog = setInterval(() => { if (Date.now()-heartbeat > 6500) cleanup(); },1500);
  return true;
})()
