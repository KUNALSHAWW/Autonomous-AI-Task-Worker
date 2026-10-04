// Turns the live DOM into a compact, numbered text view for the model.
// Interactive elements get [n] ids (stored as data-aw attributes so the
// next action can find them), tables become pipe rows, hidden stuff is dropped,
// and anything covered by an overlay is flagged so the model knows to deal with
// the overlay first.
(prev) => {
  const prevSeen = new Set(prev || []);
  document.querySelectorAll('[data-aw]').forEach(e => e.removeAttribute('data-aw'));
  let n = 0;
  const out = [];
  const fields = {};
  const seen = [];
  let line = '';
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'SVG', 'IFRAME', 'META', 'LINK']);
  const BLOCK = new Set(['DIV', 'P', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'MAIN', 'NAV', 'FORM', 'UL', 'OL', 'LI',
    'TR', 'BR', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE', 'TABLE', 'CAPTION', 'LABEL', 'FIELDSET', 'BLOCKQUOTE', 'HR', 'DL', 'DT', 'DD', 'ASIDE']);

  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const flush = () => { const t = clean(line); if (t) out.push(t); line = ''; };
  const visible = el => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.tagName === 'OPTION';
  };
  const labelOf = el => {
    let l = el.getAttribute('aria-label');
    if (!l && el.labels && el.labels.length) l = el.labels[0].innerText;
    if (!l && el.id) { const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (lab) l = lab.innerText; }
    if (!l) l = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
    return clean(l);
  };
  // The field's own name, without hint text nested inside the label (used for matching and policy)
  const nameOf = el => {
    const lab = (el.labels && el.labels[0]) || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`));
    if (lab) {
      const own = clean(Array.from(lab.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
      if (own) return own;
    }
    return labelOf(el);
  };
  const coveredBy = el => {
    const r = el.getBoundingClientRect();
    const x = r.left + Math.min(r.width / 2, 20), y = r.top + Math.min(r.height / 2, 10);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return '';
    const top = document.elementFromPoint(x, y);
    if (!top || el.contains(top) || top.contains(el)) return '';
    const dlg = top.closest('[role=dialog], .overlay, .modal, dialog') || top;
    if (dlg.contains(el)) return '';
    return clean(dlg.innerText).slice(0, 60) || dlg.tagName.toLowerCase();
  };
  const isInteractive = el => {
    const t = el.tagName;
    if (['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(t)) return !(t === 'A' && !el.getAttribute('href'));
    const role = el.getAttribute('role');
    return ['button', 'link', 'checkbox', 'tab', 'menuitem', 'option', 'radio', 'switch'].includes(role) || el.hasAttribute('onclick');
  };
  const describe = el => {
    const t = el.tagName;
    const sig = [];
    let s;
    if (t === 'A') {
      const href = el.getAttribute('href') || '';
      s = `link "${clean(el.innerText) || labelOf(el)}" -> ${href}`;
    } else if (t === 'BUTTON' || el.getAttribute('role') === 'button' || (t === 'INPUT' && ['submit', 'button'].includes(el.type))) {
      s = `button "${clean(el.innerText) || el.value || labelOf(el)}"`;
    } else if (t === 'SELECT') {
      const opts = Array.from(el.options).map(o => clean(o.text));
      const sel = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      s = `select "${labelOf(el)}" selected="${sel}" options=[${opts.slice(0, 40).join(' | ')}${opts.length > 40 ? ' | ...' : ''}]`;
      if (el.name) fields[el.name] = nameOf(el);
    } else if (t === 'INPUT' && ['checkbox', 'radio'].includes(el.type)) {
      s = `${el.type} "${labelOf(el)}"${el.checked ? ' checked' : ''}`;
      if (el.name) fields[el.name] = nameOf(el);
    } else if (t === 'INPUT' || t === 'TEXTAREA') {
      if (el.type === 'hidden') return null;
      const kind = el.type === 'password' ? 'password' : (t === 'TEXTAREA' ? 'textarea' : 'textbox');
      const val = el.type === 'password' ? (el.value ? '********' : '') : el.value;
      s = `${kind} "${labelOf(el)}" value="${(val || '').slice(0, 120)}"`;
      if (el.required) s += ' required';
      if (el.name) fields[el.name] = nameOf(el);
    } else {
      s = `${el.getAttribute('role') || t.toLowerCase()} "${clean(el.innerText).slice(0, 80)}"`;
    }
    sig.push(s);
    const cov = coveredBy(el);
    if (cov) s += ` (BLOCKED: covered by overlay "${cov}")`;
    return s;
  };

  const renderTable = tbl => {
    flush();
    const rows = Array.from(tbl.rows);
    const cap = tbl.caption ? clean(tbl.caption.innerText) : '';
    if (cap) out.push(cap);
    rows.slice(0, 40).forEach(tr => {
      const cells = Array.from(tr.cells).map(td => {
        const parts = [];
        const walkCell = node => {
          for (const c of node.childNodes) {
            if (c.nodeType === 3) { const t = clean(c.textContent); if (t) parts.push(t); }
            else if (c.nodeType === 1 && visible(c)) {
              if (isInteractive(c)) { parts.push(tag(c)); } else walkCell(c);
            }
          }
        };
        walkCell(td);
        return parts.join(' ');
      });
      out.push('| ' + cells.join(' | ') + ' |');
    });
    if (rows.length > 40) out.push(`(${rows.length - 40} more rows not shown)`);
  };

  const tag = el => {
    const d = describe(el);
    if (!d) return '';
    n += 1;
    el.setAttribute('data-aw', String(n));
    const key = d.replace(/value="[^"]*"/, '').replace(/selected="[^"]*"/, '');
    seen.push(key);
    const isNew = prev && prev.length && !prevSeen.has(key);
    return `${isNew ? '*' : ''}[${n}] ${d}`;
  };

  const walk = node => {
    for (const c of node.childNodes) {
      if (c.nodeType === 3) { line += ' ' + c.textContent; continue; }
      if (c.nodeType !== 1 || SKIP.has(c.tagName) || !visible(c)) continue;
      if (c.tagName === 'TABLE') { renderTable(c); continue; }
      if (c.tagName === 'LABEL' && c.control && !c.contains(c.control)) { flush(); continue; }
      if (isInteractive(c)) {
        const t = tag(c);
        if (t) { if (['A'].includes(c.tagName)) line += ' ' + t; else { flush(); out.push(t); } }
        continue;
      }
      const block = BLOCK.has(c.tagName);
      if (block) flush();
      if (/^H[1-6]$/.test(c.tagName)) { out.push('#'.repeat(+c.tagName[1]) + ' ' + clean(c.innerText)); continue; }
      if (c.getAttribute('role') === 'alert') { out.push('ALERT: ' + clean(c.innerText)); continue; }
      if (c.getAttribute('role') === 'dialog') { out.push('DIALOG OPEN:'); }
      if (c.tagName === 'LI') line += '- ';
      walk(c);
      if (block) flush();
    }
  };
  walk(document.body);
  flush();
  // collapse consecutive duplicates
  const lines = out.filter((l, i) => l !== out[i - 1]);
  return { text: lines.join('\n'), fields, seen, title: document.title, count: n };
}
