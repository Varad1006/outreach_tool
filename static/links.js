/* Link tool: select text in a textarea, click "Add link", it becomes [text](url).
   Markup: <button data-link-for="textareaId"> and <div data-preview-for="textareaId" data-domains="1">. */
(() => {
  const esc = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const A = (h, t) => `<a href="${h.replace(/"/g, '%22')}" target="_blank" rel="noopener">${t}</a>`;
  const TLD = 'com|site|ai|io|co|net|org|in|ae|uk|ca|dev|app|tech|agency|me|xyz|info|biz|us|eu|online|store|studio|digital|solutions|consulting|services';
  window.renderLinks = (text, domains) => {
    const keep = []; const hold = h => `\u0000${keep.push(h) - 1}\u0000`;
    let h = esc(text || '');
    h = h.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g, (_, t, u) => hold(A(u, t)));
    h = h.replace(/https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g, u => hold(A(u, u)));
    if (domains) {
      h = h.replace(/(^|[^\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)/g, (_, p, e) => p + hold(A('mailto:' + e, e)));
      h = h.replace(new RegExp(`(^|[^@\\/\\w.-])((?:www\\.)?[a-z0-9][a-z0-9-]*(?:\\.[a-z0-9-]+)*\\.(?:${TLD})(?:\\/[^\\s<]*)?)(?![\\w-])`, 'gi'),
        (_, p, d) => p + hold(A('https://' + d, d)));
    }
    return h.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[+i]).replace(/\n/g, '<br>');
  };

  document.querySelectorAll('[data-preview-for]').forEach(el => {
    const ta = document.getElementById(el.dataset.previewFor);
    const upd = () => { el.innerHTML = window.renderLinks(ta.value, el.dataset.domains === '1') || '<span class="muted">Empty</span>'; };
    ta.addEventListener('input', upd); upd();
  });

  document.querySelectorAll('[data-link-for]').forEach(btn => {
    const ta = document.getElementById(btn.dataset.linkFor);
    const box = document.createElement('div');
    box.className = 'linkbox hide';
    box.innerHTML = `<div class="linkgrid">
        <div><label class="field" style="margin-top:0">Text to show</label><input type="text" class="lt" placeholder="e.g. see the demos"></div>
        <div><label class="field" style="margin-top:0">Link (URL)</label><input type="url" class="lu" placeholder="https://www.varad-tech.site/demos"></div></div>
      <div class="row" style="margin-top:10px"><button type="button" class="primary sm li">Insert link</button>
        <button type="button" class="ghost sm lc">Cancel</button>
        <span class="hint" style="margin:0">Tip: select the words in the text first; they become the clickable part.</span></div>`;
    ta.insertAdjacentElement('afterend', box);
    const lt = box.querySelector('.lt'), lu = box.querySelector('.lu');
    let sel = [0, 0];
    btn.addEventListener('click', () => {
      sel = [ta.selectionStart, ta.selectionEnd];
      const picked = ta.value.slice(sel[0], sel[1]).trim();
      const md = picked.match(/^\[([^\]]+)\]\(([^)]+)\)$/);          // editing an existing link
      lt.value = md ? md[1] : picked;
      lu.value = md ? md[2] : /^https?:\/\//i.test(picked) ? picked : /^[\w.-]+\.[a-z]{2,}(\/\S*)?$/i.test(picked) ? 'https://' + picked : '';
      box.classList.remove('hide'); (lt.value ? lu : lt).focus(); lu.select();
    });
    const insert = () => {
      let url = lu.value.trim(); const text = (lt.value.trim() || url).replace(/[\[\]]/g, '');
      if (!url) { lu.focus(); return; }
      if (!/^(https?:\/\/|mailto:)/i.test(url)) url = (url.includes('@') ? 'mailto:' : 'https://') + url;
      const md = `[${text}](${url})`;
      ta.value = ta.value.slice(0, sel[0]) + md + ta.value.slice(sel[1]);
      ta.focus(); ta.setSelectionRange(sel[0] + md.length, sel[0] + md.length);
      box.classList.add('hide'); ta.dispatchEvent(new Event('input'));
    };
    box.querySelector('.li').addEventListener('click', insert);
    box.querySelector('.lc').addEventListener('click', () => box.classList.add('hide'));
    [lt, lu].forEach(i => i.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); insert(); } }));
  });
})();
