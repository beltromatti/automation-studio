// Collects a navigable, indexed snapshot of the page for non-vision LLM agents.
// Returns an ordered list of nodes: interactive elements (each tagged with a
// stable data-hb-index attribute so actions can target it) interleaved with the
// visible text around them, mirroring document order so an agent understands
// layout from text alone. Descends into open shadow roots and same-origin iframes.
(opts) => {
  const MAX_NODES = (opts && opts.maxNodes) || 1200;
  // Atomic controls are leaves: their inner text is just a label, so we emit
  // them and stop. Container roles (dialog, menu, listbox, group, ...) are NOT
  // here — they wrap other controls and must be descended into.
  const ATOMIC_TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','OPTION','SUMMARY']);
  const ATOMIC_ROLES = new Set(['button','link','checkbox','radio','option','switch','tab','menuitem','menuitemcheckbox','menuitemradio','treeitem','slider','spinbutton','textbox','searchbox','combobox']);
  const NESTED_CONTROL_SEL = 'a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=menuitem],[role=tab],[role=option],[role=switch],[contenteditable=true]';
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','SVG','PATH']);

  const vw = window.innerWidth, vh = window.innerHeight;

  function clearAll(root) {
    let els;
    try { els = root.querySelectorAll('[data-hb-index]'); } catch (e) { return; }
    els.forEach(e => e.removeAttribute('data-hb-index'));
    // shadow roots
    try {
      root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) clearAll(e.shadowRoot); });
    } catch (e) {}
  }
  clearAll(document);

  // Visibility is split into two questions, because pruning a whole subtree on
  // an ancestor's box is wrong: children with position:absolute/fixed escape a
  // zero-sized parent, and a child may re-enable visibility:visible. Only
  // display:none and opacity:0 truly hide a subtree.
  function pruneSubtree(style) {
    return style.display === 'none' || parseFloat(style.opacity) === 0;
  }
  function selfVisible(style, rect) {
    return style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }

  function isAtomic(el) {
    if (ATOMIC_TAGS.has(el.tagName)) return true;
    const role = el.getAttribute('role');
    if (role && ATOMIC_ROLES.has(role)) return true;
    if (el.isContentEditable) return true;
    return false;
  }
  function isClickable(el) {  // clickable but possibly a container
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    return false;
  }

  function clip(s, n) { s = (s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) + '…' : s; }

  function accName(el) {
    const tag = el.tagName;
    let n = el.getAttribute('aria-label') || '';
    if (!n && el.getAttribute('aria-labelledby')) {
      const ids = el.getAttribute('aria-labelledby').split(/\s+/);
      n = ids.map(id => { const e = el.ownerDocument.getElementById(id); return e ? e.innerText : ''; }).join(' ');
    }
    if (!n && tag === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button') n = el.value || '';
      n = n || el.getAttribute('placeholder') || '';
      if (!n && el.labels && el.labels.length) n = el.labels[0].innerText;
      if (!n) n = el.getAttribute('name') || '';
    }
    if (!n && tag === 'TEXTAREA') n = el.getAttribute('placeholder') || el.getAttribute('name') || '';
    if (!n && tag === 'IMG') n = el.getAttribute('alt') || '';
    if (!n) n = el.getAttribute('title') || '';
    if (!n) n = el.innerText || el.textContent || '';
    return clip(n, 140);
  }

  function attrs(el) {
    const a = {};
    const tag = el.tagName;
    const role = el.getAttribute('role'); if (role) a.role = role;
    if (tag === 'A') { const h = el.getAttribute('href'); if (h) a.href = clip(h, 90); }
    if (tag === 'INPUT') {
      a.type = (el.getAttribute('type') || 'text').toLowerCase();
      if (el.checked) a.checked = true;
      if (el.value && a.type !== 'password') a.value = clip(el.value, 50);
    }
    if (tag === 'SELECT') a.value = el.value;
    const exp = el.getAttribute('aria-expanded'); if (exp) a.expanded = exp;
    const sel = el.getAttribute('aria-selected'); if (sel) a.selected = sel;
    const chk = el.getAttribute('aria-checked'); if (chk) a.checked = chk;
    if (el.disabled) a.disabled = true;
    const ph = el.getAttribute('placeholder'); if (ph && !a.value) a.placeholder = clip(ph, 50);
    return a;
  }

  function xpath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== 'HTML') {
      let ix = 1, sib = cur.previousElementSibling;
      while (sib) { if (sib.tagName === cur.tagName) ix++; sib = sib.previousElementSibling; }
      parts.unshift(cur.tagName.toLowerCase() + '[' + ix + ']');
      cur = cur.parentElement || (cur.getRootNode() && cur.getRootNode().host) || null;
    }
    return '/' + parts.join('/');
  }

  function inViewport(rect) { return rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw; }

  let index = 0;
  let visited = 0;
  const MAX_VISIT = 20000;
  const nodes = [];

  function emit(el, rect, framePath) {
    const idx = index++;
    el.setAttribute('data-hb-index', String(idx));
    nodes.push({
      type: 'element', index: idx, tag: el.tagName.toLowerCase(),
      name: accName(el), attrs: attrs(el),
      inViewport: inViewport(rect),
      center: [Math.round(rect.x + rect.width / 2), Math.round(rect.y + rect.height / 2)],
      xpath: xpath(el), frame: framePath,
    });
  }

  function walk(container, framePath, depth) {
    if (index >= MAX_NODES || depth > 50 || visited >= MAX_VISIT) return;
    const children = container.children ? container.children : [];
    for (const el of children) {
      if (index >= MAX_NODES || visited >= MAX_VISIT) return;
      if (SKIP_TAGS.has(el.tagName)) continue;
      visited++;
      let style;
      try { style = el.ownerDocument.defaultView.getComputedStyle(el); } catch (e) { continue; }
      if (!style || pruneSubtree(style)) continue;  // subtree genuinely not rendered
      const rect = el.getBoundingClientRect();
      const vis = selfVisible(style, rect);

      // 1) Atomic controls (native + ARIA widgets): emit, never descend.
      if (isAtomic(el)) {
        if (vis) emit(el, rect, framePath);
        continue;
      }

      // 2) Custom clickable element with no nested controls: it IS the control.
      if (vis && isClickable(el) && !el.querySelector(NESTED_CONTROL_SEL)) {
        emit(el, rect, framePath);
        continue;
      }

      // 3) Otherwise a container (incl. clickable wrappers like role=dialog):
      //    emit its own visible text, then descend to reach nested controls.
      if (vis) {
        let directText = '';
        for (const c of el.childNodes) { if (c.nodeType === 3) directText += c.textContent; }
        directText = clip(directText, 200);
        if (directText && directText.length > 1) nodes.push({ type: 'text', text: directText });
      }
      if (el.shadowRoot) { try { walk(el.shadowRoot, framePath + '/shadow', depth + 1); } catch (e) {} }
      if (el.tagName === 'IFRAME') {
        try { const doc = el.contentDocument; if (doc) walk(doc.body || doc.documentElement, framePath + '/iframe', depth + 1); } catch (e) {}
        continue;
      }
      walk(el, framePath, depth + 1);
    }
  }

  walk(document.body || document.documentElement, '', 0);

  return {
    url: location.href,
    title: document.title,
    scrollY: Math.round(window.scrollY),
    innerHeight: vh,
    innerWidth: vw,
    scrollHeight: Math.round(document.documentElement.scrollHeight),
    hasMoreBelow: (window.scrollY + vh) < (document.documentElement.scrollHeight - 4),
    numElements: index,
    truncated: index >= MAX_NODES,
    nodes,
  };
}
