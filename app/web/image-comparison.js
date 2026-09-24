/* Original / Qwen edit comparison. No image processing or network writes. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const localImage = value => typeof value === 'string' && value.startsWith('/images/') && !value.includes('\\');

  // Compatibility with the already-running server: derive the same read-only
  // grouping from its public reference URLs. No process restart is required.
  const sourceCache = new Map(), sourcePending = new Set(), sourceMissing = new Set();
  let comparisonCards = new Map();
  window.comparisonHiddenCount = 0;
  function sourceName(entry) {
    const value = String(entry.ref_image_path || entry.requested_ref_image_path || entry.ref_image || '');
    let name;
    try {
      if (value.startsWith('/images/')) name = decodeURIComponent(value.slice(8).split('?')[0]);
      else if (value && !/[\\/]/.test(value) && !value.includes(':')) name = value;
    } catch (_) { return ''; }
    return name && !/[\\/]/.test(name) && name !== '.' && name !== '..' ? name : '';
  }
  function publicImage(entry) {
    let url = localImage(entry.image_path) ? entry.image_path : '/images/' + encodeURIComponent(entry.image_filename);
    if (entry.image_revision) url += (url.includes('?') ? '&' : '?') + 'v=' + encodeURIComponent(entry.image_revision);
    return {filename:entry.image_filename, url, model_name:entry.model_name, width:entry.width || 0,
      height:entry.height || 0, date:entry.date || '', time:entry.time || '', created_at:entry.created_at || 0};
  }
  function loadSource(name) {
    if (sourcePending.has(name) || sourceMissing.has(name)) return;
    sourcePending.add(name);
    fetch('/api/images/' + encodeURIComponent(name), {cache:'no-store'})
      .then(async response => {
        if (!response.ok) throw new Error('source_unavailable');
        const entry = await response.json();
        if (entry.image_filename !== name) throw new Error('source_mismatch');
        sourceCache.set(name, entry);
        window.renderActivePhotoView?.();
        window.updateBadges?.();
      }).catch(() => sourceMissing.add(name))
      .finally(() => sourcePending.delete(name));
  }
  window.prepareImageComparisonEntries = function(entries) {
    const records = new Map(entries.filter(e => e.image_filename).map(e => [e.image_filename, e]));
    const parents = new Map();
    // Include ancestors outside the current page using the read-only detail API.
    const inspect = Array.from(records.values());
    for (let i = 0; i < inspect.length; i++) {
      const entry = inspect[i];
      if (!String(entry.model_name || '').toLowerCase().startsWith('qwen-image-2.1')) continue;
      const name = sourceName(entry);
      if (!name || name === entry.image_filename) continue;
      if (!records.has(name)) {
        if (sourceCache.has(name)) { records.set(name, sourceCache.get(name)); inspect.push(sourceCache.get(name)); }
        else { loadSource(name); continue; }
      }
      parents.set(entry.image_filename, name);
    }
    const children = new Map();
    for (const name of parents.keys()) {
      const visited = new Set();
      let current = name;
      while (parents.has(current) && !visited.has(current)) { visited.add(current); current = parents.get(current); }
      if (visited.has(current) || current === name) continue;
      if (!children.has(current)) children.set(current, []);
      children.get(current).push(name);
    }
    const hidden = new Set(), cards = new Map(records);
    for (const [root, names] of children) {
      names.sort((a,b) => {
        const aa = records.get(a), bb = records.get(b);
        return String(aa.date || '').localeCompare(String(bb.date || '')) || String(aa.time || '').localeCompare(String(bb.time || '')) || a.localeCompare(b);
      });
      const entry = records.get(root), edits = names.map(name => ({...publicImage(records.get(name)), source_filename:parents.get(name)}));
      if (!edits.length) continue;
      cards.set(root, {...entry, image_comparison:{before:publicImage(entry), after:edits[edits.length-1], edits, edit_count:edits.length, root_filename:root}});
      names.forEach(name => hidden.add(name));
    }
    window.comparisonHiddenCount = hidden.size;
    comparisonCards = cards;
    return [...cards.values()].filter(e => !hidden.has(e.image_filename));
  };
  window.comparisonEntryFor = (name, entries = []) => comparisonCards.get(name) || entries.find(e => e.image_filename === name);

  window.renderImageComparison = function(entry, detail = false) {
    const comparison = entry && entry.image_comparison;
    if (!comparison || !localImage(comparison.before?.url) || !localImage(comparison.after?.url)) return '';
    const before = comparison.before, after = comparison.after;
    const imageLoading = detail ? 'eager' : 'lazy';
    const edits = (comparison.edits || [after]).filter(item => localImage(item.url));
    const options = edits.map((item, index) => `<option value="${esc(item.url)}" data-filename="${esc(item.filename)}" ${item.filename === after.filename ? 'selected' : ''}>修改 ${index + 1}${item.date ? ' · ' + esc(item.date) : ''}${item.time ? ' ' + esc(item.time) : ''}</option>`).join('');
    return `<div class="compare-shell${detail ? ' compare-detail' : ''}" data-comparison-root="${esc(comparison.root_filename)}">
      <div class="image-compare" role="slider" tabindex="0" aria-label="修改前后对比，左右移动；左侧原图，右侧改后" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50" aria-valuetext="原图 50%，改后 50%" data-before-url="${esc(before.url)}" data-after-url="${esc(after.url)}" style="--compare-split:50%" title="左右移动查看修改前后；点击查看详情">
        <img class="compare-before" src="${esc(before.url)}" alt="修改前原图" loading="${imageLoading}" decoding="async" draggable="false">
        <img class="compare-after" src="${esc(after.url)}" alt="Qwen 修改后" loading="${imageLoading}" decoding="async" draggable="false">
        <span class="compare-line" aria-hidden="true"><span>↔</span></span>
        <span class="compare-label compare-label-before" aria-hidden="true">原图</span>
        <span class="compare-label compare-label-after" aria-hidden="true">改后</span>
        <span class="compare-image-error" role="status" hidden>对比图加载失败，原文件仍保留</span>
      </div>
      ${detail ? `<div class="compare-tools">
        <div class="compare-view-actions" role="group" aria-label="查看对比图片">
          <button class="compare-view-btn compare-view-before" type="button" data-compare-open="before" title="打开原图大图">
            <svg class="compare-view-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m4 17 5-5 4 4 3-3 5 5"/></svg>
            <span>查看原图</span>
          </button>
          <button class="compare-view-btn compare-view-after" type="button" data-compare-open="after" title="打开修改后的大图">
            <svg class="compare-view-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3L12 3Z"/><path d="M20 2v4m-2-2h4"/></svg>
            <span>查看改后</span>
          </button>
        </div>
        ${edits.length > 1 ? `<select class="compare-version" aria-label="选择改图版本">${options}</select>` : '<span class="compare-model">Qwen Q8</span>'}
      </div>` : ''}
    </div>`;
  };

  function setSplit(element, value) {
    const percent = +Math.max(0, Math.min(100, Number(value) || 0)).toFixed(4);
    if (element._compareLastPercent === percent) return;
    element._compareLastPercent = percent;
    element.style.setProperty('--compare-split', `${percent}%`);
    const rounded = Math.round(percent);
    if (element._compareAria !== rounded) {
      element.setAttribute('aria-valuenow', String(rounded));
      element.setAttribute('aria-valuetext', `原图 ${rounded}%，改后 ${100-rounded}%`);
      element._compareAria = rounded;
    }
    element.classList.toggle('compare-edge-left', percent < 8);
    element.classList.toggle('compare-edge-right', percent > 92);
    element.classList.toggle('compare-at-start', percent === 0);
    element.classList.toggle('compare-at-end', percent === 100);
  }
  // Coalesce pointer samples into one visual update per animation frame.
  const pendingMoves = new Map();
  const bounds = new WeakMap();
  const activeDrags = new Map();
  let moveFrame = 0, boundsRevision = 0, lastDragEnd = null;
  function invalidateBounds() { boundsRevision++; }
  window.addEventListener('resize', invalidateBounds, {passive:true});
  document.addEventListener('scroll', invalidateBounds, {capture:true, passive:true});
  document.addEventListener('animationend', invalidateBounds, {passive:true});
  document.addEventListener('transitionend', invalidateBounds, {passive:true});
  window.visualViewport?.addEventListener('resize', invalidateBounds, {passive:true});
  const boundsObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(invalidateBounds) : null;
  function flushMoves() {
    moveFrame = 0;
    const updates = [];
    // Read before writing. Edge tolerance is in CSS pixels, not image pixels.
    for (const [element, sample] of pendingMoves) {
      if (!element.isConnected) { boundsObserver?.unobserve(element); continue; }
      let cached = bounds.get(element);
      if (!cached || cached.revision !== boundsRevision) {
        cached = {box:element.getBoundingClientRect(), revision:boundsRevision};
        bounds.set(element, cached);
      }
      const {left, width} = cached.box;
      if (!width) continue;
      let clientX = sample.clientX;
      // A fast horizontal exit may have no final in-bounds pointermove.
      // A vertical exit must not overwrite the last real horizontal sample.
      if (Number.isFinite(sample.exitX) && (sample.exitX <= left || sample.exitX >= left + width)) clientX = sample.exitX;
      if (!Number.isFinite(clientX)) continue;
      const offset = clientX - left;
      const snap = Math.min(sample.pointerType === 'touch' ? 20 : 12, width * 0.08);
      const value = offset <= snap ? 0 : offset >= width - snap ? 100 : offset / width * 100;
      updates.push([element, value]);
    }
    pendingMoves.clear();
    for (const [element, value] of updates) setSplit(element, value);
  }
  function track(element, event, boundaryOnly = false) {
    if (!Number.isFinite(event.clientX)) return;
    if (boundaryOnly) {
      const sample = pendingMoves.get(element) || {clientX:null, pointerType:event.pointerType};
      sample.exitX = event.clientX;
      pendingMoves.set(element, sample);
    } else {
      pendingMoves.set(element, {clientX:event.clientX, pointerType:event.pointerType});
    }
    if (!moveFrame) moveFrame = requestAnimationFrame(flushMoves);
  }
  function slider(event) { return event.target?.closest?.('.image-compare'); }
  function dragSlider(event) {
    const element = activeDrags.get(event.pointerId);
    if (element && !element.isConnected) {
      activeDrags.delete(event.pointerId);
      pendingMoves.delete(element);
      boundsObserver?.unobserve(element);
      return null;
    }
    return element || slider(event);
  }
  function updateDirection(state, event) {
    const dx = Math.abs(event.clientX - state.x), dy = Math.abs(event.clientY - state.y);
    if (!state.moved && !state.vertical && Math.max(dx, dy) > 5) {
      if (event.pointerType !== 'mouse' && dy > dx) state.vertical = true;
      else state.moved = true;
    }
  }
  function finishDrag(element, event, commit) {
    const state = element?._comparePointer;
    if (!state || state.id !== event.pointerId) return;
    if (commit) updateDirection(state, event);
    if (state.moved || state.vertical) {
      element._compareSuppressClickUntil = Date.now() + 500;
      if (commit) lastDragEnd = {id:state.id, x:event.clientX, y:event.clientY, until:Date.now() + 500};
    }
    if (commit && !state.vertical && (state.moved || event.pointerType === 'mouse')) {
      bounds.delete(element); // The modal may have moved since pointerdown.
      track(element, event);
    } else if (!commit) {
      pendingMoves.delete(element);
    }
    activeDrags.delete(state.id);
    element._comparePointer = null;
    element.classList.remove('compare-dragging');
    boundsObserver?.unobserve(element);
    try { if (element.hasPointerCapture(state.id)) element.releasePointerCapture(state.id); } catch (_) {}
  }
  const pointerOptions = {capture:true, passive:true};
  document.addEventListener('pointerover', event => {
    const element = slider(event);
    if (!element || element.contains(event.relatedTarget)) return;
    bounds.delete(element);
    boundsObserver?.observe(element);
  }, pointerOptions);
  document.addEventListener('pointerout', event => {
    const element = slider(event);
    if (!element || element.contains(event.relatedTarget) || element._comparePointer) return;
    if (event.pointerType === 'mouse') {
      bounds.delete(element);
      track(element, event, true);
    }
    boundsObserver?.unobserve(element);
  }, pointerOptions);

  document.addEventListener('pointerdown', event => {
    const element = slider(event);
    if (!element || event.button !== 0 || event.isPrimary === false || element._comparePointer) return;
    bounds.delete(element);
    boundsObserver?.observe(element);
    element.classList.add('compare-dragging');
    element._comparePointer = {id:event.pointerId, x:event.clientX, y:event.clientY, moved:false, vertical:false};
    activeDrags.set(event.pointerId, element);
    lastDragEnd = null;
    if (event.pointerType === 'mouse') track(element, event);
    try { element.setPointerCapture(event.pointerId); } catch (_) { /* Document tracking remains active. */ }
  }, pointerOptions);
  document.addEventListener('pointermove', event => {
    const element = dragSlider(event);
    if (!element) return;
    const state = element._comparePointer;
    if (state) {
      if (state.id !== event.pointerId) return;
      if (event.pointerType === 'mouse' && event.buttons === 0) {
        finishDrag(element, event, true); // Recover a pointerup missed outside the window.
        return;
      }
      updateDirection(state, event);
      if (state.vertical || (event.pointerType !== 'mouse' && !state.moved)) return;
    } else if (event.pointerType !== 'mouse') return;
    track(element, event);
  }, pointerOptions);
  function release(event) {
    finishDrag(dragSlider(event), event, event.type === 'pointerup');
  }
  document.addEventListener('pointerup', release, pointerOptions);
  document.addEventListener('pointercancel', release, pointerOptions);
  document.addEventListener('lostpointercapture', event => {
    const element = activeDrags.get(event.pointerId);
    if (element) bounds.delete(element); // Keep the drag until up/cancel, even over the backdrop.
  }, pointerOptions);
  window.addEventListener('blur', () => {
    for (const [pointerId, element] of activeDrags) finishDrag(element, {pointerId}, false);
  });
  document.addEventListener('click', event => {
    const element = slider(event);
    const endedOutside = lastDragEnd && event.detail !== 0 && Date.now() < lastDragEnd.until
      && (event.pointerId == null || event.pointerId === lastDragEnd.id)
      && Math.abs(event.clientX - lastDragEnd.x) <= 4 && Math.abs(event.clientY - lastDragEnd.y) <= 4;
    if (endedOutside || (element && Date.now() < (element._compareSuppressClickUntil || 0))) {
      event.preventDefault();
      event.stopImmediatePropagation();
      lastDragEnd = null;
    }
  }, true);
  document.addEventListener('keydown', event => {
    const element = slider(event);
    if (!element) return;
    let value = Number(element.getAttribute('aria-valuenow'));
    if (event.key === 'ArrowLeft') value -= 5;
    else if (event.key === 'ArrowRight') value += 5;
    else if (event.key === 'Home') value = 0;
    else if (event.key === 'End') value = 100;
    else return;
    event.preventDefault();
    event.stopPropagation();
    pendingMoves.delete(element);
    setSplit(element, value);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest?.('[data-compare-open]');
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const element = button.closest('.compare-shell').querySelector('.image-compare');
    const url = button.dataset.compareOpen === 'before' ? element.dataset.beforeUrl : element.dataset.afterUrl;
    if (localImage(url) && typeof window.openFullscreenImg === 'function') window.openFullscreenImg(url);
  }, true); // Run before the existing detail modal stops click propagation.
  document.addEventListener('change', event => {
    if (!event.target.matches('.compare-version')) return;
    const select = event.target, element = select.closest('.compare-shell').querySelector('.image-compare');
    if (!localImage(select.value)) return;
    element.dataset.afterUrl = select.value;
    element.classList.remove('compare-after-failed');
    element.querySelector('.compare-image-error').hidden = true;
    element.querySelector('.compare-after').src = select.value;
    setSplit(element, 50);
  });
  document.addEventListener('error', event => {
    const image = event.target;
    if (!image.matches?.('.compare-before, .compare-after')) return;
    const element = image.closest('.image-compare');
    element.classList.add(image.classList.contains('compare-after') ? 'compare-after-failed' : 'compare-before-failed');
    element.querySelector('.compare-image-error').hidden = false;
  }, true);
})();
