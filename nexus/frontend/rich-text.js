/* Safe, dependency-free renderer for the limited Markdown emitted by models.
   It creates DOM nodes and never injects HTML strings. */
(function () {
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function inline(text) {
    const fragment = document.createDocumentFragment();
    String(text).split(/(\*\*[^*]+\*\*)/g).filter(Boolean).forEach(part => {
      if (part.startsWith('**') && part.endsWith('**')) fragment.append(el('strong', '', part.slice(2, -2)));
      else fragment.append(document.createTextNode(part));
    });
    return fragment;
  }

  function cells(line) {
    return line.trim().replace(/^\||\|$/g, '').split('|').map(value => value.trim());
  }

  function isSeparator(line) {
    const parts = cells(line);
    return parts.length > 1 && parts.every(value => /^:?-{3,}:?$/.test(value));
  }

  function render(text) {
    const root = el('div', 'rich-text');
    const lines = String(text || '').replace(/\r/g, '').split('\n');
    let index = 0;
    while (index < lines.length) {
      const raw = lines[index].trim();
      if (!raw) { index += 1; continue; }
      if (raw.includes('|') && index + 1 < lines.length && isSeparator(lines[index + 1])) {
        const wrap = el('div', 'rich-table-wrap'), table = el('table', 'rich-table');
        const head = el('thead'), headRow = el('tr');
        cells(raw).forEach(value => { const th = el('th'); th.append(inline(value)); headRow.append(th); });
        head.append(headRow); table.append(head); index += 2;
        const body = el('tbody');
        while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
          const row = el('tr'); cells(lines[index]).forEach(value => { const td = el('td'); td.append(inline(value)); row.append(td); });
          body.append(row); index += 1;
        }
        table.append(body); wrap.append(table); root.append(wrap); continue;
      }
      const heading = raw.match(/^(#{2,4})\s+(.+)$/);
      if (heading) { const h = el(heading[1].length === 2 ? 'h3' : 'h4'); h.append(inline(heading[2])); root.append(h); index += 1; continue; }
      if (/^[-*]\s+/.test(raw)) {
        const list = el('ul');
        while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
          const li = el('li'); li.append(inline(lines[index].replace(/^\s*[-*]\s+/, ''))); list.append(li); index += 1;
        }
        root.append(list); continue;
      }
      if (/^\d+[.)]\s+/.test(raw)) {
        const list = el('ol');
        while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
          const li = el('li'); li.append(inline(lines[index].replace(/^\s*\d+[.)]\s+/, ''))); list.append(li); index += 1;
        }
        root.append(list); continue;
      }
      // A line that is nothing but bold text (e.g. "**结论**" or "**依据**：")
      // is a section label, not prose. Render it as a styled subheading so the
      // card reads like a structured answer instead of a wall of paragraphs.
      if (/^\*\*[^*]{1,20}\*\*\s*[:：]?\s*$/.test(raw) || /^\*\*[^*]{1,20}\*\*[:：]/.test(raw)) {
        const label = el('h4', 'rich-label');
        label.append(inline(raw.replace(/[:：]\s*$/, '')));
        const trailing = raw.match(/\*\*[^*]{1,20}\*\*[:：]\s*(.+)$/);
        root.append(label);
        if (trailing && trailing[1]) {
          const rest = el('p'); rest.append(inline(trailing[1])); root.append(rest);
        }
        index += 1; continue;
      }
      const paragraph = el('p'); paragraph.append(inline(raw)); root.append(paragraph); index += 1;
    }
    return root;
  }

  window.NexusRichText = Object.freeze({ render });
}());
