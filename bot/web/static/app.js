(() => {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (sel, root = document) => root.querySelector(sel);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const GAP = 12;
  const MEDALS = ['🥇', '🥈', '🥉'];

  const state = {
    cases: [], caseIndex: 0, demo: false, demoAllowed: true, needSub: [],
    busy: false, stripItems: [], x: 0, user: null, topTab: 'recent',
  };

  // ---------- утилиты ----------
  const haptic = {
    select: () => tg?.HapticFeedback?.selectionChanged(),
    impact: (style = 'medium') => tg?.HapticFeedback?.impactOccurred(style),
    notify: (type) => tg?.HapticFeedback?.notificationOccurred(type),
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const star = () => '<svg class="star"><use href="#i-star"/></svg>';
  const pill = (price) => `<span class="pill">${star()}${price}</span>`;
  const alert = (msg) => (tg?.showAlert ? tg.showAlert(msg) : window.alert(msg));

  function formatChance(value) {
    if (value >= 10) return `${+value.toFixed(2)}%`;
    if (value >= 1) return `${+value.toFixed(2)}%`;
    return `${+value.toPrecision(3)}%`;
  }

  function ago(ts) {
    const diff = Math.max(0, Date.now() / 1000 - ts);
    if (diff < 60) return 'только что';
    if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
    return new Date(ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `tma ${tg?.initData || ''}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* пустой ответ */ }
    if (!res.ok) {
      const err = new Error(data.message || 'Нет связи с сервером, попробуйте ещё раз');
      err.code = data.error;
      throw err;
    }
    return data;
  }

  // ---------- картинки подарков (оригинальные Lottie-стикеры Telegram) ----------
  const animationData = new Map(); // gift_id -> Promise<object|null>
  const snapshots = new Map();     // gift_id -> Promise<string|null>

  function lottieReady() {
    if (window.lottie) return Promise.resolve(true);
    return new Promise((resolve) => {
      const started = Date.now();
      const timer = setInterval(() => {
        if (window.lottie || Date.now() - started > 5000) { clearInterval(timer); resolve(!!window.lottie); }
      }, 50);
    });
  }

  function loadAnimation(prize) {
    if (prize.media !== 'json') return Promise.resolve(null);
    if (!animationData.has(prize.gift_id)) {
      animationData.set(prize.gift_id, fetch(`/api/gift/${prize.gift_id}`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null));
    }
    return animationData.get(prize.gift_id);
  }

  async function renderSnapshot(prize) {
    if (prize.media === 'json') {
      const [data, ready] = await Promise.all([loadAnimation(prize), lottieReady()]);
      if (data && ready) {
        const box = document.createElement('div');
        box.style.cssText = 'position:fixed;left:-10000px;top:0;width:256px;height:256px';
        document.body.append(box);
        try {
          const anim = window.lottie.loadAnimation({ container: box, renderer: 'svg', loop: false, autoplay: false, animationData: data });
          anim.goToAndStop(0, true);
          const svg = box.querySelector('svg');
          svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
          svg.setAttribute('width', '256');
          svg.setAttribute('height', '256');
          const markup = new XMLSerializer().serializeToString(svg);
          anim.destroy();
          return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
        } catch (_) { /* упадём на превью */ } finally { box.remove(); }
      }
    }
    if (prize.media === 'webp') return `/api/gift/${prize.gift_id}`;
    if (prize.thumb) return `/api/gift/${prize.gift_id}/thumb`;
    return null;
  }

  function snapshot(prize) {
    if (!snapshots.has(prize.gift_id)) snapshots.set(prize.gift_id, renderSnapshot(prize));
    return snapshots.get(prize.gift_id);
  }

  /** Эмодзи-заглушка, которая сама заменяется на картинку подарка, когда та готова. */
  function giftNode(prize, extraClass = '') {
    const holder = document.createElement('div');
    holder.className = `gift emoji ${extraClass}`;
    holder.textContent = prize.emoji;
    snapshot(prize).then((src) => {
      if (!src) return;
      const img = new Image();
      img.className = `gift ${extraClass}`;
      img.alt = prize.emoji;
      img.decoding = 'async';
      img.onload = () => holder.replaceWith(img);
      img.src = src;
    });
    return holder;
  }

  // ---------- кейсы и призы ----------
  const currentCase = () => state.cases[state.caseIndex];

  function renderCases() {
    const box = $('#cases');
    box.innerHTML = state.cases.map((c, i) => `
      <button class="seg ${i === state.caseIndex ? 'active' : ''}" data-index="${i}" type="button" role="tab">
        ${esc(c.name)} ${c.price} ${star()}
      </button>`).join('');
    box.style.display = state.cases.length > 1 ? '' : 'none';
  }

  function renderPrizes() {
    const c = currentCase();
    const box = $('#prizes');
    box.replaceChildren();
    for (const p of [...c.prizes].sort((a, b) => a.chance - b.chance)) {
      const card = document.createElement('div');
      card.className = 'prize';
      card.innerHTML = `<div class="chance">${formatChance(p.chance)} 🎲</div><div class="box"></div>`;
      const inner = $('.box', card);
      inner.append(giftNode(p));
      inner.insertAdjacentHTML('beforeend', pill(p.price));
      box.append(card);
    }
  }

  function renderButton() {
    const c = currentCase();
    $('#spin-price').innerHTML = state.demo ? '· демо' : `${c ? c.price : ''} ${star()}`;
  }

  function selectCase(index) {
    if (state.busy || index === state.caseIndex) return;
    state.caseIndex = index;
    haptic.select();
    renderCases();
    renderPrizes();
    renderButton();
    resetStrip();
  }

  // ---------- рулетка ----------
  const strip = $('#strip');
  const roulette = $('#roulette');

  function randomPrize(prizes) {
    const total = prizes.reduce((s, p) => s + p.chance, 0);
    let r = Math.random() * total;
    for (const p of prizes) { if ((r -= p.chance) <= 0) return p; }
    return prizes[prizes.length - 1];
  }

  function itemNode(prize) {
    const el = document.createElement('div');
    el.className = 'item';
    el.append(giftNode(prize));
    el.insertAdjacentHTML('beforeend', pill(prize.price));
    return el;
  }

  function metrics() {
    const first = strip.firstElementChild;
    const w = first ? first.getBoundingClientRect().width : 140;
    return { w, step: w + GAP, center: roulette.clientWidth / 2 };
  }

  function setX(value) {
    state.x = value;
    strip.style.transform = `translate3d(${value}px,0,0)`;
  }

  const xForIndex = (i, jitter = 0) => { const m = metrics(); return m.center - (i * m.step + m.w / 2) - jitter; };
  const indexAt = (x) => { const m = metrics(); return Math.floor((m.center - x) / m.step); };

  function renderStrip() { strip.replaceChildren(...state.stripItems.map(itemNode)); }

  function resetStrip() {
    const c = currentCase();
    if (!c) return;
    state.stripItems = Array.from({ length: 9 }, () => randomPrize(c.prizes));
    renderStrip();
    setX(xForIndex(4));
  }

  function animate(from, to, duration, ease) {
    return new Promise((resolve) => {
      const start = performance.now();
      let lastIndex = indexAt(from);
      const frame = (now) => {
        const t = Math.min(1, (now - start) / duration);
        const value = from + (to - from) * ease(t);
        setX(value);
        const idx = indexAt(value);
        if (idx !== lastIndex) { lastIndex = idx; haptic.select(); }
        if (t < 1) requestAnimationFrame(frame); else resolve();
      };
      requestAnimationFrame(frame);
    });
  }

  async function spinTo(prize) {
    const c = currentCase();
    // Оставляем видимые сейчас карточки и дописываем ленту, в конце которой — выигрыш.
    const current = Math.round((metrics().center - state.x - metrics().w / 2) / metrics().step);
    const keepFrom = Math.max(0, current - 4);
    state.stripItems = state.stripItems.slice(keepFrom);
    const start = current - keepFrom;
    const target = start + 44;
    while (state.stripItems.length < target + 6) state.stripItems.push(randomPrize(c.prizes));
    state.stripItems[target] = prize;
    renderStrip();
    setX(xForIndex(start));

    const { w } = metrics();
    const jitter = (Math.random() - 0.5) * w * 0.6;
    await animate(state.x, xForIndex(target, jitter), 5600, (t) => 1 - Math.pow(1 - t, 4.2));
    await animate(state.x, xForIndex(target), 380, (t) => 1 - Math.pow(1 - t, 3));
    haptic.impact('heavy');
    strip.children[target]?.classList.add('win');
  }

  // ---------- прокрутка ----------
  const button = $('#spin-button');

  function setLoading(on) { button.classList.toggle('busy', on); }
  function setBusy(on) {
    state.busy = on;
    button.disabled = on;
    $('#demo-toggle').disabled = on;
    document.querySelectorAll('#cases .seg').forEach((b) => { b.disabled = on; });
  }

  const openInvoice = (url) => new Promise((resolve) => tg.openInvoice(url, resolve));

  async function waitSpin(spinId) {
    for (let i = 0; i < 60; i += 1) {
      const res = await api(`/api/spin/${spinId}`);
      if (res.status === 'refunded' || (res.prize && res.status !== 'created')) return res;
      await sleep(i < 10 ? 500 : 1000);
    }
    throw new Error('Оплата прошла, результат вот-вот появится во вкладке «Профиль».');
  }

  async function onSpin() {
    const c = currentCase();
    if (state.busy || !c) return;
    if (state.needSub.length) { showSubscribe(); return; }
    haptic.impact('light');
    setBusy(true);
    setLoading(true);
    try {
      if (state.demo) {
        const { prize } = await api('/api/demo', { case_id: c.id });
        setLoading(false);
        await spinTo(prize);
        showWin(prize, 'demo');
        return;
      }
      const { spin_id: spinId, invoice } = await api('/api/spin', { case_id: c.id });
      const status = await openInvoice(invoice);
      if (status !== 'paid') {
        if (status === 'failed') alert('Оплата не прошла. Попробуйте ещё раз.');
        return;
      }
      const res = await waitSpin(spinId);
      setLoading(false);
      if (res.status === 'refunded') { alert('Звёзды вернулись на ваш счёт — попробуйте ещё раз.'); return; }
      await spinTo(res.prize);
      // Подарок отправляется только сейчас — когда рулетка уже остановилась.
      let final = res;
      try { final = await api(`/api/spin/${spinId}/reveal`, {}); } catch (_) { final = { status: 'pending' }; }
      showWin(res.prize, final.status);
    } catch (err) {
      if (err.code === 'need_sub') { await refreshSub(); showSubscribe(); } else alert(err.message);
    } finally {
      setLoading(false);
      setBusy(false);
    }
  }

  // ---------- шторка ----------
  const sheet = $('#sheet');
  const backdrop = $('#backdrop');
  let sheetAnimation = null;

  function openSheet(html) {
    sheet.innerHTML = html;
    backdrop.classList.add('open');
    sheet.classList.add('open');
  }

  function closeSheet() {
    sheet.classList.remove('open');
    backdrop.classList.remove('open');
    if (sheetAnimation) { sheetAnimation.destroy(); sheetAnimation = null; }
  }
  backdrop.addEventListener('click', closeSheet);

  function confetti() {
    const box = document.createElement('div');
    box.className = 'confetti';
    const colors = ['#FFD84D', '#F29D1B', '#5eaef0', '#34c759', '#ff5a8a', '#b67cff'];
    for (let i = 0; i < 70; i += 1) {
      const piece = document.createElement('i');
      piece.style.left = `${Math.random() * 100}%`;
      piece.style.background = colors[i % colors.length];
      piece.style.setProperty('--dx', `${(Math.random() - 0.5) * 160}px`);
      piece.style.setProperty('--rot', `${Math.random() * 900 - 450}deg`);
      piece.style.animationDuration = `${1.8 + Math.random() * 1.6}s`;
      piece.style.animationDelay = `${Math.random() * 0.3}s`;
      box.append(piece);
    }
    document.body.append(box);
    setTimeout(() => box.remove(), 4000);
  }

  async function showWin(prize, status) {
    const demo = status === 'demo';
    const note = {
      demo: 'Это демо-прокрутка. Выключите демо-режим, чтобы играть по-настоящему.',
      sent: 'Подарок уже в вашем профиле Telegram 🎉',
    }[status] || 'Подарок отправим в ближайшее время — пришлём уведомление в чат.';
    openSheet(`
      <div class="win-gift" id="win-gift"></div>
      <h3>${demo ? 'Мог бы быть вашим!' : 'Поздравляем!'}</h3>
      ${pill(prize.price)}
      <p>${note}</p>
      <button class="big-button" id="again" type="button">${demo ? 'Крутить ещё' : 'Испытать удачу ещё раз'}</button>
      <button class="secondary" id="close-sheet" type="button">Закрыть</button>`);
    haptic.notify('success');
    if (!demo) confetti();

    const box = $('#win-gift');
    const data = await loadAnimation(prize);
    if (data && (await lottieReady())) {
      sheetAnimation = window.lottie.loadAnimation({ container: box, renderer: 'svg', loop: true, autoplay: true, animationData: data });
    } else {
      box.append(giftNode(prize, 'win-gift'));
    }
    $('#again').addEventListener('click', () => { closeSheet(); onSpin(); });
    $('#close-sheet').addEventListener('click', closeSheet);
    if (!demo) { profileLoaded = false; topLoaded = false; }
  }

  // ---------- обязательная подписка ----------
  async function refreshSub() {
    const { need_sub: needSub } = await api('/api/check_sub', {});
    state.needSub = needSub;
    return needSub;
  }

  function showSubscribe() {
    const rows = state.needSub.map((ch, i) => `
      <div class="channel"><span class="name">${esc(ch.title)}</span>
        <button type="button" data-channel="${i}">Открыть</button></div>`).join('');
    openSheet(`
      <div style="font-size:56px;margin:6px 0">📢</div>
      <h3>Почти готово!</h3>
      <p>Чтобы крутить рулетку, подпишитесь на каналы:</p>
      ${rows}
      <button class="big-button" id="check-sub" type="button" style="margin-top:10px">
        <span class="label">Я подписался</span><span class="spinner"></span></button>`);
    sheet.querySelectorAll('[data-channel]').forEach((b) => b.addEventListener('click', () => {
      const url = state.needSub[+b.dataset.channel].url;
      if (tg?.openTelegramLink && /^https:\/\/t\.me\//.test(url)) tg.openTelegramLink(url); else window.open(url, '_blank');
    }));
    const check = $('#check-sub');
    check.addEventListener('click', async () => {
      check.classList.add('busy');
      try {
        const left = await refreshSub();
        if (left.length) { haptic.notify('error'); showSubscribe(); } else { haptic.notify('success'); closeSheet(); }
      } catch (err) { alert(err.message); } finally { check.classList.remove('busy'); }
    });
  }

  // ---------- топ ----------
  let topLoaded = false;
  let topData = null;

  function renderTop() {
    const list = $('#top-list');
    if (!topData) { list.innerHTML = '<div class="empty"><div class="boot-spinner" style="margin:auto"></div></div>'; return; }
    const rows = state.topTab === 'recent' ? topData.recent : topData.top;
    if (!rows.length) {
      list.innerHTML = '<div class="empty"><div class="big">🎲</div>Пока пусто — стань первым!</div>';
      return;
    }
    list.replaceChildren();
    rows.forEach((r, i) => {
      const row = document.createElement('div');
      row.className = `list-row ${r.me ? 'me' : ''}`;
      if (state.topTab === 'recent') {
        row.append(giftNode(r));
        row.insertAdjacentHTML('beforeend', `
          <div class="body"><div class="name">${esc(r.name)}</div>
          <div class="sub">«${esc(r.case)}» · ${ago(r.at)}</div></div>${pill(r.price)}`);
      } else {
        row.innerHTML = `<div class="rank ${i < 3 ? 'medal' : ''}">${MEDALS[i] || i + 1}</div>
          <div class="body"><div class="name">${esc(r.name)}${r.me ? ' · вы' : ''}</div>
          <div class="sub">${r.spins} прокрут. · лучший приз ${r.best} ${star()}</div></div>${pill(r.won)}`;
      }
      list.append(row);
    });
  }

  async function loadTop() {
    if (topLoaded) return;
    topData = null;
    renderTop();
    try { topData = await api('/api/top'); topLoaded = true; } catch (err) { topData = { recent: [], top: [] }; alert(err.message); }
    renderTop();
  }

  $('#top-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg');
    if (!btn || btn.dataset.tab === state.topTab) return;
    state.topTab = btn.dataset.tab;
    haptic.select();
    document.querySelectorAll('#top-tabs .seg').forEach((b) => b.classList.toggle('active', b === btn));
    renderTop();
  });

  // ---------- профиль ----------
  let profileLoaded = false;
  const STATUS = {
    sent: ['sent', 'Получен'], pending: ['pending', 'В пути'], paid: ['pending', 'В пути'],
    delivering: ['pending', 'В пути'], refunded: ['refunded', 'Возврат'],
  };

  async function loadProfile() {
    const user = state.user;
    const avatar = $('#avatar');
    if (user.photo_url) avatar.style.backgroundImage = `url("${user.photo_url}")`;
    else avatar.textContent = (user.name || '?').trim()[0] || '?';
    $('#profile-name').textContent = user.name;
    if (profileLoaded) return;

    const history = $('#history');
    history.innerHTML = '<div class="empty"><div class="boot-spinner" style="margin:auto"></div></div>';
    let data;
    try { data = await api('/api/profile'); profileLoaded = true; } catch (err) { history.innerHTML = ''; alert(err.message); return; }

    $('#stats').innerHTML = `
      <div class="stat"><b>${data.spins}</b><span>прокруток</span></div>
      <div class="stat"><b>${data.won} ${star()}</b><span>выиграно</span></div>
      <div class="stat"><b>${data.best ? `${esc(data.best.emoji)} ${data.best.price}` : '—'}</b><span>лучший приз</span></div>`;

    if (!data.history.length) {
      history.innerHTML = '<div class="empty"><div class="big">🎲</div>Вы ещё не крутили рулетку<br><button type="button" id="go-play">Испытать удачу</button></div>';
      $('#go-play').addEventListener('click', () => showScreen('play'));
      return;
    }
    history.replaceChildren();
    for (const s of data.history) {
      const [cls, label] = STATUS[s.status] || ['pending', s.status];
      const row = document.createElement('div');
      row.className = 'list-row';
      if (s.prize) row.append(giftNode(s.prize));
      const d = new Date(s.at * 1000);
      const date = `${d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })} ${d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
      row.insertAdjacentHTML('beforeend', `
        <div class="body"><div class="name">Выигрыш ${s.prize ? s.prize.price : '—'} ${star()}</div>
        <div class="sub">«${esc(s.case)}» за ${s.price} ${star()} · ${date}</div></div>
        <span class="chip ${cls}">${label}</span>`);
      history.append(row);
    }
  }

  // ---------- навигация ----------
  function showScreen(name) {
    document.querySelectorAll('.screen').forEach((s) => s.classList.toggle('active', s.id === `screen-${name}`));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.screen === name));
    window.scrollTo(0, 0);
    if (name === 'top') loadTop();
    if (name === 'profile') loadProfile();
    if (name === 'play') requestAnimationFrame(() => setX(xForIndex(Math.max(0, indexAt(state.x)))));
  }

  $('#tabbar').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab || tab.classList.contains('active')) return;
    haptic.select();
    showScreen(tab.dataset.screen);
  });
  $('#cases').addEventListener('click', (e) => { const b = e.target.closest('.seg'); if (b) selectCase(+b.dataset.index); });
  button.addEventListener('click', onSpin);
  $('#demo-toggle').addEventListener('change', (e) => { state.demo = e.target.checked; haptic.select(); renderButton(); });
  $('#nft-link').addEventListener('click', () => {
    const text = 'Выигранный подарок появляется в вашем профиле Telegram. Его можно оставить, обменять на звёзды '
      + 'или улучшить до уникального NFT — коллекционной версии с особым фоном, узором и номером.';
    if (tg?.showPopup) tg.showPopup({ title: 'Что такое NFT?', message: text, buttons: [{ type: 'ok' }] }); else alert(text);
  });
  window.addEventListener('resize', () => setX(xForIndex(Math.max(0, Math.round((metrics().center - state.x - metrics().w / 2) / metrics().step)))));

  // ---------- запуск ----------
  function fatal(message) {
    $('#boot').innerHTML = `<div class="error"><div style="font-size:48px;margin-bottom:8px">🎁</div>${esc(message)}</div>`;
  }

  async function boot() {
    if (!tg || !tg.initData) { fatal('Откройте рулетку из Telegram — через кнопку в боте.'); return; }
    tg.ready();
    tg.expand();
    tg.disableVerticalSwipes?.();
    try { tg.setHeaderColor('secondary_bg_color'); tg.setBackgroundColor('secondary_bg_color'); } catch (_) { /* старый клиент */ }

    let data;
    try { data = await api('/api/init', {}); } catch (err) { fatal(err.message); return; }
    state.user = data.user;
    state.cases = data.cases;
    state.demoAllowed = data.demo;
    state.needSub = data.need_sub;
    $('#demo-row').style.display = data.demo ? '' : 'none';

    if (!state.cases.length) {
      fatal('Рулетка пока не настроена — загляните чуть позже.');
      return;
    }
    renderCases();
    renderPrizes();
    renderButton();
    $('#boot').classList.add('hidden');
    requestAnimationFrame(resetStrip);
    if (state.needSub.length) setTimeout(showSubscribe, 400);
  }

  boot();
})();
