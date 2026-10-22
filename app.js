/* CIVICLENS browser application. API data lives in SQLite; session tokens stay in this tab. */
(() => {
  'use strict';

  const API = '/api';
  const DEFAULT_CENTER = [26.8500, 80.9500];
  const DRAFT_DB_NAME = 'civiclens-offline-drafts-v1';
  let draftDatabasePromise;
  let modalReturnFocus = null;
  let syncingDrafts = false;
  const state = {
    config: null, user: null, token: sessionStorage.getItem('civiclens_token') || '',
    reports: [], mine: [], adminReports: [], map: null, markers: null, pickMap: null,
    pickMarker: null, activeView: 'home', detailId: null, detailReport: null,
    analysis: null, photo: null, adminTab: 'queue', continueAfterAuth: '', searchTimer: null,
    language: document.documentElement.lang || 'en-IN',
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value = '') => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const titleCase = value => String(value || '').replaceAll('_', ' ').replace(/\b\w/g, char => char.toUpperCase());
  const byId = id => document.getElementById(id);

  function openDraftDatabase() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('Offline drafts are not supported by this browser.'));
    if (!draftDatabasePromise) draftDatabasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DRAFT_DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore('reports', { keyPath: 'id' });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Could not open local draft storage.'));
    });
    return draftDatabasePromise;
  }

  async function draftStore(mode, action) {
    const db = await openDraftDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('reports', mode);
      const store = tx.objectStore('reports');
      let request;
      try { request = action(store); } catch (error) { reject(error); return; }
      tx.oncomplete = () => resolve(request?.result);
      tx.onerror = () => reject(tx.error || request?.error || new Error('Could not update offline drafts.'));
      tx.onabort = () => reject(tx.error || new Error('Offline draft update was interrupted.'));
    });
  }

  async function listOfflineDrafts() { return (await draftStore('readonly', store => store.getAll())) || []; }
  async function saveOfflineDraftRecord(draft) { await draftStore('readwrite', store => store.put(draft)); }
  async function deleteOfflineDraft(id) { await draftStore('readwrite', store => store.delete(id)); }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
      headers.set('Content-Type', 'application/json');
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(`${API}${path}`, { ...options, headers });
    const contentType = response.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(body?.detail || body?.error || `Request failed (${response.status})`);
    return body;
  }

  function toast(message, kind = 'success', duration = 4200) {
    const node = document.createElement('div');
    node.className = `toast toast-${kind}`;
    node.innerHTML = `<span class="toast-icon">${kind === 'error' ? '!' : kind === 'info' ? 'i' : '✓'}</span><span>${esc(message)}</span><button aria-label="Dismiss notification">×</button>`;
    $('#toastRegion').append(node);
    const remove = () => node.remove();
    $('button', node).addEventListener('click', remove);
    window.setTimeout(remove, duration);
  }

  function setButtonBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.innerHTML;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.innerHTML = `<span class="spinner"></span>${esc(label || 'Working…')}`;
    } else {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      if (button.dataset.originalText) button.innerHTML = button.dataset.originalText;
      delete button.dataset.originalText;
    }
  }

  function showError(element, message) {
    element.textContent = message;
    element.classList.remove('hidden');
  }
  function clearError(element) { element.textContent = ''; element.classList.add('hidden'); }

  function fmtDate(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat(state.language, { dateStyle: options.dateStyle || 'medium', ...(options.time ? { timeStyle: 'short' } : {}) }).format(date);
  }
  function timeAgo(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Recently';
    const mins = Math.max(1, Math.round((Date.now() - date.getTime()) / 60000));
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return days < 7 ? `${days}d ago` : fmtDate(value, { dateStyle: 'short' });
  }
  function statusClass(status) { return `status-${String(status || '').toLowerCase().replaceAll(' ', '-')}`; }
  function severityClass(severity) { return `severity-${String(severity || '').toLowerCase()}`; }
  function badge(text, kind) { return `<span class="badge ${kind}">${esc(text)}</span>`; }

  function setView(view, updateHash = true) {
    if (!['home', 'map', 'dashboard', 'admin'].includes(view)) view = 'home';
    if (view === 'admin' && state.user?.role !== 'admin') {
      view = 'admin';
      loadAdminGate();
    }
    state.activeView = view;
    $$('.view').forEach(section => section.classList.toggle('view-active', section.id === `view-${view}`));
    $$('.nav-link').forEach(button => button.classList.toggle('active', button.dataset.viewLink === view));
    $('#mainNav').classList.remove('mobile-open');
    $('#mobileMenu').setAttribute('aria-expanded', 'false');
    if (updateHash) history.replaceState(null, '', `#${view}`);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (view === 'map') loadMapView();
    if (view === 'dashboard') loadDashboard();
    if (view === 'admin') state.user?.role === 'admin' ? loadAdmin() : loadAdminGate();
  }

  function showModal(id) {
    const modal = byId(id);
    if (!$('.modal-backdrop:not(.hidden)')) modalReturnFocus = document.activeElement;
    modal.classList.remove('hidden');
    document.body.classList.add('modal-open');
    modal.setAttribute('tabindex', '-1');
    window.setTimeout(() => {
      const heading = $('h1,h2,h3', modal);
      if (heading) { heading.tabIndex = -1; heading.focus(); }
      else modal.focus();
    }, 40);
  }
  function closeModal(id) {
    byId(id)?.classList.add('hidden');
    if (!$('.modal-backdrop:not(.hidden)')) {
      document.body.classList.remove('modal-open');
      if (modalReturnFocus?.isConnected) modalReturnFocus.focus();
      modalReturnFocus = null;
    }
  }
  function closeTopModal() {
    const opened = $('.modal-backdrop:not(.hidden)');
    if (opened) closeModal(opened.id);
  }

  function openAuth(mode = 'login', next = '') {
    state.continueAfterAuth = next;
    setAuthMode(mode);
    $('#reportLoginNotice').classList.toggle('hidden', next !== 'report');
    $('#aadhaarStatusMessage').classList.add('hidden');
    clearError($('#authError'));
    showModal('authModal');
  }
  function setAuthMode(mode) {
    const register = mode === 'register';
    $('#loginTab').classList.toggle('selected', !register);
    $('#registerTab').classList.toggle('selected', register);
    $('#nameField').classList.toggle('hidden', !register);
    $('#authName').required = register;
    $('#authPassword').autocomplete = register ? 'new-password' : 'current-password';
    $('#aadhaarLoginPanel').classList.toggle('hidden', register);
    $('#authTitle').textContent = register ? 'Create your account' : 'Sign in to your account';
    $('#authSubtitle').textContent = register ? 'Save reports and get updates as your city responds.' : 'Track reports and help your neighborhood be heard.';
    $('#authSubmit').textContent = register ? 'Create account' : 'Sign in';
  }

  function updateAccountUI() {
    $('#signInButton').classList.toggle('hidden', !!state.user);
    $('#accountChip').classList.toggle('hidden', !state.user);
    $$('.admin-nav').forEach(button => button.classList.toggle('hidden', state.user?.role !== 'admin'));
    if (state.user) {
      $('#accountName').textContent = state.user.name.split(' ')[0];
      $('#accountAvatar').textContent = state.user.name.trim().charAt(0).toUpperCase();
      $('#profileName').textContent = state.user.name;
      $('#profileEmail').textContent = state.user.email;
      $('#profileAvatar').textContent = state.user.name.trim().charAt(0).toUpperCase();
      $('#dashboardSignedOut').classList.add('hidden');
      $('#dashboardContent').classList.remove('hidden');
    } else {
      $('#dashboardSignedOut').classList.remove('hidden');
      $('#dashboardContent').classList.add('hidden');
    }
  }

  function setSession(session) {
    state.token = session.token;
    state.user = session.user;
    sessionStorage.setItem('civiclens_token', state.token);
    updateAccountUI();
    closeModal('authModal');
    toast(`Welcome${state.user.name ? `, ${state.user.name.split(' ')[0]}` : ''}.`);
    if (state.continueAfterAuth === 'report') openReport();
    else if (state.activeView === 'dashboard') loadDashboard();
    else if (state.activeView === 'admin' && state.user.role === 'admin') loadAdmin();
    if (navigator.onLine) syncQueuedDrafts();
    state.continueAfterAuth = '';
  }

  async function loadConfig() {
    try {
      state.config = await api('/config');
      const categoryOptions = state.config.categories.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
      $('#mapCategory').insertAdjacentHTML('beforeend', categoryOptions);
      const reportCategoryOptions = (state.config.reportCategories || state.config.categories).map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
      $('#reportCategory').insertAdjacentHTML('beforeend', reportCategoryOptions);
      const statusOptions = state.config.statuses.map(status => `<option value="${esc(status)}">${esc(status)}</option>`).join('');
      $('#mapStatus').insertAdjacentHTML('beforeend', statusOptions);
      $('#adminStatus').insertAdjacentHTML('beforeend', statusOptions);
      if (state.config.demoMode) $('#demoRibbon').classList.remove('hidden');
      $('#analysisModeLabel').textContent = state.config.aiMode === 'configured' ? 'Configured vision model' : 'Demo mode';
    } catch (error) {
      toast(`Could not connect to CIVICLENS: ${error.message}`, 'error', 8000);
    }
  }

  async function loadIdentity() {
    if (!state.token) { updateAccountUI(); return; }
    try {
      state.user = await api('/profile');
      updateAccountUI();
    } catch {
      state.token = '';
      state.user = null;
      sessionStorage.removeItem('civiclens_token');
      updateAccountUI();
    }
  }

  async function loadHome() {
    try {
      const [stats, reports] = await Promise.all([api('/stats'), api('/reports')]);
      $('#statTotal').textContent = stats.total.toLocaleString();
      $('#statVerified').textContent = stats.communityConfirmations.toLocaleString();
      $('#statResolved').textContent = stats.resolved.toLocaleString();
      $('#statSource').textContent = stats.demoRecordsIncluded ? 'Includes clearly labeled synthetic demo records' : 'Current records from CIVICLENS';
      $('#mapReportCount').textContent = stats.total.toLocaleString();
      state.reports = reports;
      renderRecent(reports.slice(0, 3));
    } catch (error) {
      renderRecent([]);
      toast(`Could not load civic records: ${error.message}`, 'error', 8000);
    }
  }

  function reportCard(report, compact = false) {
    const score = report.impact?.score ?? '—';
    return `<article class="report-card ${compact ? 'report-card-compact' : ''}" data-open-report="${esc(report.id)}" tabindex="0" role="button" aria-label="View report ${esc(report.id)}">
      <div class="report-card-top"><span class="category-tag">${esc(report.categoryLabel || titleCase(report.category))}${report.isDemo ? ' · DEMO RECORD' : ''}</span>${badge(report.status, statusClass(report.status))}</div>
      <h3>${esc(report.title)}</h3><p class="report-card-description">${esc(report.description)}</p>
      <div class="report-card-location"><span>⌖</span>${esc(report.location)}${report.ward ? ` · ${esc(report.ward)}` : ''}</div>
      <div class="report-card-bottom">${badge(report.severity, severityClass(report.severity))}<span class="report-time">${timeAgo(report.createdAt)}</span><span class="card-score" title="CIVICLENS ImpactScore">◎ ${esc(score)}</span></div>
    </article>`;
  }

  function renderRecent(reports) {
    $('#recentReports').innerHTML = reports.length ? reports.map(report => reportCard(report, true)).join('') : `<div class="empty-state">No reports have been submitted yet. A neighborhood issue can start the first.</div>`;
  }

  function ensureMap() {
    if (state.map) return true;
    if (!window.L) {
      $('#mapLoading').textContent = 'Map library could not load. The report list is still available below.';
      return false;
    }
    state.map = L.map('civicMap', { zoomControl: false, scrollWheelZoom: false }).setView(DEFAULT_CENTER, 12);
    L.control.zoom({ position: 'bottomright' }).addTo(state.map);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>',
    }).addTo(state.map);
    state.markers = window.L.markerClusterGroup ? L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 46 }) : L.layerGroup();
    state.map.addLayer(state.markers);
    state.map.on('click', event => {
      if (!$('#reportModal').classList.contains('hidden')) setPickCoordinates(event.latlng.lat, event.latlng.lng);
    });
    return true;
  }

  function markerIcon(report) {
    const css = ['Resolved', 'Rejected'].includes(report.status) ? 'marker-resolved' : `marker-${String(report.severity).toLowerCase()}`;
    return L.divIcon({ className: 'civic-marker-shell', html: `<span class="civic-marker ${css}" aria-label="${esc(report.severity)} severity"></span>`, iconSize: [22, 22], iconAnchor: [11, 11] });
  }

  function mapQuery(search = '') {
    const params = new URLSearchParams();
    [['category', 'mapCategory'], ['severity', 'mapSeverity'], ['status', 'mapStatus'], ['from', 'mapFrom'], ['to', 'mapTo']]
      .forEach(([key, id]) => { if (byId(id).value) params.set(key, byId(id).value); });
    if ($('#mapOpenOnly').checked) params.set('open_only', 'true');
    if (search) params.set('search', search);
    return params;
  }

  function renderMapMarkers(reports) {
    if (!state.map || !state.markers) return;
    state.markers.clearLayers();
    reports.forEach(report => {
      if (!Number.isFinite(Number(report.lat)) || !Number.isFinite(Number(report.lng))) return;
      const marker = L.marker([report.lat, report.lng], { icon: markerIcon(report), title: report.title });
      marker.bindPopup(`<div class="map-popup"><span>${esc(report.categoryLabel)}</span><strong>${esc(report.title)}</strong><small>${esc(report.location)}</small><div>${badge(report.severity, severityClass(report.severity))} ${badge(report.status, statusClass(report.status))}</div><button data-popup-report="${esc(report.id)}">View report ↗</button></div>`);
      marker.addTo(state.markers);
    });
  }

  async function loadMapView() {
    const hasMap = ensureMap();
    if (hasMap) requestAnimationFrame(() => state.map.invalidateSize());
    $('#mapLoading').classList.remove('hidden');
    const params = mapQuery($('#mapSearch').value.trim());
    try {
      const reports = await api(`/reports?${params}`);
      state.reports = reports;
      $('#mapResultsCount').textContent = `${reports.length} report${reports.length === 1 ? '' : 's'}`;
      $('#mapList').innerHTML = reports.length ? reports.map(report => reportCard(report)).join('') : '<div class="empty-state">No reports match these filters. Try widening the dates or clearing a filter.</div>';
      renderMapMarkers(reports);
    } catch (error) {
      toast(`Could not refresh the map: ${error.message}`, 'error');
    } finally { $('#mapLoading').classList.add('hidden'); }
  }

  async function searchMapLocation() {
    const query = $('#mapSearch').value.trim();
    if (!query) return toast('Enter a street, landmark or neighborhood to search.', 'info');
    const button = $('#searchLocation');
    setButtonBusy(button, true, 'Searching…');
    try {
      const response = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`, { headers: { 'Accept-Language': navigator.language || 'en' } });
      if (!response.ok) throw new Error('Location search is unavailable right now.');
      const results = await response.json();
      if (!results.length) throw new Error('No matching place was found. Try a nearby landmark.');
      ensureMap();
      state.map.setView([Number(results[0].lat), Number(results[0].lon)], 15);
    } catch (error) { toast(error.message, 'error'); }
    finally { setButtonBusy(button, false); }
  }

  function loadDashboard() {
    updateAccountUI();
    if (!state.user) return;
    loadOfflineDrafts();
    if (!navigator.onLine) return;
    Promise.all([api('/reports/mine'), api('/notifications')]).then(([reports, notifications]) => {
      state.mine = reports;
      const open = reports.filter(report => !['Resolved', 'Rejected'].includes(report.status)).length;
      const resolved = reports.filter(report => report.status === 'Resolved').length;
      $('#dashboardStats').innerHTML = `<div class="summary-card"><span>YOUR REPORTS</span><strong>${reports.length}</strong></div><div class="summary-card"><span>IN PROGRESS</span><strong>${open}</strong></div><div class="summary-card"><span>RESOLVED</span><strong>${resolved}</strong></div>`;
      $('#myReports').innerHTML = reports.length ? reports.map(report => reportCard(report, true)).join('') : '<div class="empty-state">You have not submitted a report yet. Start with a photo or a few details.</div>';
      $('#notifications').innerHTML = notifications.length ? notifications.slice(0, 8).map(item => `<article class="notification-item"><span class="notification-dot ${statusClass(item.status)}"></span><div><strong>${esc(item.title)}</strong><p>${esc(item.detail || `Status updated to ${item.status}.`)}</p><small>${esc(item.status)} · ${timeAgo(item.createdAt)}</small></div></article>`).join('') : '<div class="empty-state empty-small">Status updates will appear here.</div>';
    }).catch(error => toast(`Could not load your dashboard: ${error.message}`, 'error'));
  }

  function loadAdminGate() {
    $('#adminDenied').classList.toggle('hidden', state.user?.role === 'admin');
    $('#adminContent').classList.toggle('hidden', state.user?.role !== 'admin');
  }

  async function loadAdmin() {
    loadAdminGate();
    if (state.user?.role !== 'admin') return;
    try {
      const [reports, analytics] = await Promise.all([api('/admin/reports'), api('/analytics')]);
      state.adminReports = reports;
      const active = reports.filter(r => !['Resolved', 'Rejected'].includes(r.status)).length;
      const high = reports.filter(r => ['Critical', 'High'].includes(r.severity) && !['Resolved', 'Rejected'].includes(r.status)).length;
      const resolved = reports.filter(r => r.status === 'Resolved').length;
      const overdue = reports.filter(report => report.responseSlaStatus === 'overdue').length;
      $('#adminStats').innerHTML = `<div class="summary-card"><span>TOTAL REPORTS</span><strong>${reports.length}</strong></div><div class="summary-card summary-alert"><span>HIGH PRIORITY OPEN</span><strong>${high}</strong></div><div class="summary-card"><span>ACTIVE WORK</span><strong>${active}</strong></div><div class="summary-card summary-good"><span>RESOLVED</span><strong>${resolved}</strong></div><div class="summary-card sla-summary"><span>48-HOUR RESPONSE OVERDUE</span><strong>${overdue}</strong></div>`;
      renderAdminRows();
      renderAnalytics(analytics);
      loadAudit();
    } catch (error) {
      if (error.message.includes('Administrator access')) {
        state.user = null; state.token = ''; sessionStorage.removeItem('civiclens_token'); updateAccountUI(); loadAdminGate();
      } else toast(`Could not load city operations: ${error.message}`, 'error');
    }
  }

  function renderAdminRows() {
    const search = $('#adminSearch').value.trim().toLowerCase();
    const status = $('#adminStatus').value;
    const severity = $('#adminSeverity').value;
    const rows = state.adminReports.filter(report => (!status || report.status === status) && (!severity || report.severity === severity) && (!search || `${report.id} ${report.title} ${report.location}`.toLowerCase().includes(search)))
      .sort((a, b) => (b.impact?.score || 0) - (a.impact?.score || 0));
    $('#adminCount').textContent = rows.length;
    $('#adminEmpty').classList.toggle('hidden', rows.length > 0);
    $('#adminReportRows').innerHTML = rows.map(report => `<tr data-admin-report="${esc(report.id)}" tabindex="0"><td><span class="table-title">${report.isSensitive ? '<span class="sensitive-tag">🔒 PRIVATE · </span>' : ''}${esc(report.title)}</span><small>${esc(report.id)} · ${esc(report.categoryLabel)}</small></td><td><span class="score-pill">◎ ${esc(report.impact?.score ?? '—')}</span><small>${esc(report.severity)} severity</small></td><td>${badge(report.status, statusClass(report.status))}<small class="sla-cell">${report.responseSlaStatus === 'overdue' ? '48h response overdue' : report.responseSlaStatus === 'met' ? 'First response on time' : report.responseSlaStatus === 'late' ? 'First response was late' : 'Response due ' + fmtDate(report.firstResponseDueAt)}</small></td><td><span class="table-title">${esc(report.location)}</span><small>${esc(report.ward || 'Area not specified')}</small></td><td><span class="table-title">${timeAgo(report.createdAt)}</span><small>${fmtDate(report.createdAt)}</small></td><td><button class="row-open" aria-label="Open ${esc(report.id)}">↗</button></td></tr>`).join('');
  }

  function renderBarChart(target, rows, labelKey, valueKey, color = 'blue') {
    if (!rows.length) { target.innerHTML = '<div class="empty-state empty-small">No stored records yet.</div>'; return; }
    const max = Math.max(1, ...rows.map(row => row[valueKey]));
    target.innerHTML = rows.map(row => `<div class="bar-row"><span class="bar-label">${esc(row[labelKey])}</span><div class="bar-track"><i class="bar-fill bar-${color}" style="width:${Math.max(3, row[valueKey] / max * 100)}%"></i></div><strong>${row[valueKey]}</strong></div>`).join('');
  }

  function renderAnalytics(data) {
    $('#analyticsSource').textContent = data.demoRecordsIncluded ? 'Includes clearly labeled synthetic demonstration reports alongside saved citizen reports.' : 'Charts update from stored citizen and administrator records.';
    renderBarChart($('#categoryChart'), data.categories, 'label', 'count', 'blue');
    const severityOrder = ['Critical', 'High', 'Medium', 'Low'];
    const severityRows = severityOrder.map(severity => ({ label: severity, count: data.severities.find(row => row.severity === severity)?.count || 0 })).filter(row => row.count > 0);
    renderBarChart($('#severityChart'), severityRows, 'label', 'count', 'teal');
    renderBarChart($('#statusChart'), data.statuses.map(row => ({ label: row.status, count: row.count })), 'label', 'count', 'teal');
    const volume = data.volume30Days || [];
    const peak = Math.max(1, ...volume.map(row => row.count));
    $('#volumeChart').innerHTML = volume.length ? `<div class="volume-bars">${volume.map(row => `<div class="volume-day" title="${esc(row.date)} · ${row.count} report${row.count === 1 ? '' : 's'}"><i style="height:${Math.max(row.count ? 8 : 3, row.count / peak * 100)}%"></i></div>`).join('')}</div><div class="volume-axis"><span>${esc(volume[0]?.date || '')}</span><span>${esc(volume.at(-1)?.date || '')}</span></div>` : '<div class="empty-state empty-small">No recent report volume.</div>';
    $('#hotspotList').innerHTML = data.hotspots.length ? data.hotspots.map((row, i) => `<div class="hotspot-row"><span class="hotspot-rank">0${i + 1}</span><span>${esc(row.ward)}</span><strong>${row.openReports}</strong></div>`).join('') : '<div class="empty-state empty-small">No unresolved hotspots.</div>';
    $('#avgResolution').textContent = data.averageResolutionHours === null ? '—' : `${data.averageResolutionHours}h`;
  }

  async function loadAudit() {
    try {
      const entries = await api('/admin/audit?limit=80');
      $('#auditEntries').innerHTML = entries.length ? entries.map(item => `<article class="audit-item"><span class="audit-symbol">↗</span><div><strong>${esc(titleCase(item.action))}</strong><p>Report <button class="inline-link" data-open-report="${esc(item.reportId)}">${esc(item.reportId)}</button>${item.actor ? ` · ${esc(item.actor)}` : ''}</p><small>${fmtDate(item.createdAt, { time: true })}${item.payload?.reason ? ` · ${esc(item.payload.reason)}` : ''}</small></div></article>`).join('') : '<div class="empty-state">Administrator actions will appear here.</div>';
    } catch (error) { $('#auditEntries').innerHTML = `<div class="empty-state">${esc(error.message)}</div>`; }
  }

  function selectAdminTab(tab) {
    state.adminTab = tab;
    $$('.admin-tab').forEach(button => button.classList.toggle('selected', button.dataset.adminTab === tab));
    ['queue', 'analytics', 'audit'].forEach(name => $(`#admin${name[0].toUpperCase()}${name.slice(1)}`).classList.toggle('hidden', name !== tab));
  }

  function openReport() {
    if (!state.user) return openAuth('login', 'report');
    resetReportForm();
    showModal('reportModal');
    window.setTimeout(() => initPickMap(), 100);
  }

  function openImpactInfo() { showModal('impactModal'); }

  function resetReportForm() {
    $('#reportForm').reset();
    state.analysis = null;
    state.photo = null;
    $('#photoPreview').classList.add('hidden');
    $('#replacePhoto').classList.add('hidden');
    $('#analyzeButton').disabled = true;
    $('#analysisResult').classList.add('hidden');
    $('#descriptionCount').textContent = '0 / 2000';
    toggleOfficialReportFields();
    if (state.pickMap) state.pickMap.setView(DEFAULT_CENTER, 13);
    if (state.pickMarker && state.pickMap) state.pickMap.removeLayer(state.pickMarker);
    state.pickMarker = null;
    clearError($('#reportError'));
  }

  function initPickMap() {
    if (!window.L) { $('#pickMap').innerHTML = '<div class="map-offline">Map could not load. Enter coordinates below instead.</div>'; return; }
    if (!state.pickMap) {
      state.pickMap = L.map('pickMap', { zoomControl: true, attributionControl: false }).setView(DEFAULT_CENTER, 13);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(state.pickMap);
      state.pickMap.on('click', event => setPickCoordinates(event.latlng.lat, event.latlng.lng));
    }
    window.setTimeout(() => state.pickMap.invalidateSize(), 100);
    const lat = Number($('#reportLat').value);
    const lng = Number($('#reportLng').value);
    if (Number.isFinite(lat) && Number.isFinite(lng) && $('#reportLat').value) {
      state.pickMap.setView([lat, lng], 16); setPickCoordinates(lat, lng);
    }
  }

  function setPickCoordinates(lat, lng) {
    $('#reportLat').value = Number(lat).toFixed(6);
    $('#reportLng').value = Number(lng).toFixed(6);
    if (window.L && state.pickMap) {
      const point = [Number(lat), Number(lng)];
      if (!state.pickMarker) state.pickMarker = L.marker(point, { draggable: true }).addTo(state.pickMap).on('dragend', event => {
        const position = event.target.getLatLng(); $('#reportLat').value = position.lat.toFixed(6); $('#reportLng').value = position.lng.toFixed(6);
      });
      else state.pickMarker.setLatLng(point);
      state.pickMap.panTo(point);
    }
  }

  async function useMyLocation() {
    if (!navigator.geolocation) return toast('Location access is unavailable in this browser. Enter a landmark and coordinates manually.', 'error');
    const button = $('#locateMe');
    button.disabled = true; button.textContent = 'Finding location…';
    navigator.geolocation.getCurrentPosition(async position => {
      const lat = position.coords.latitude, lng = position.coords.longitude;
      setPickCoordinates(lat, lng);
      try {
        const response = await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}`);
        if (response.ok) {
          const result = await response.json();
          const parts = result.address || {};
          const label = [parts.road || parts.pedestrian || parts.neighbourhood, parts.suburb || parts.city_district, parts.city || parts.town].filter(Boolean).join(', ');
          if (label && !$('#reportLocation').value) $('#reportLocation').value = label;
        }
      } catch { /* Coordinates still work if reverse geocoding is unavailable. */ }
      button.disabled = false; button.textContent = '⌖ Use my location';
    }, error => {
      button.disabled = false; button.textContent = '⌖ Use my location';
      const message = error.code === 1 ? 'Location permission was declined. Enter an address and map coordinates manually.' : 'Could not find your location. Enter an address and map coordinates manually.';
      toast(message, 'error');
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 });
  }

  function renderAnalysis(result) {
    state.analysis = result;
    const isDemo = result.mode === 'demo';
    const confidence = result.confidencePercent == null ? 'Unavailable' : `${result.confidencePercent}%`;
    $('#analysisResult').innerHTML = `<div class="analysis-head"><span class="analysis-symbol">${isDemo ? '◇' : '✳'}</span><div><strong>${isDemo ? 'Demo mode · No image AI ran' : 'Image analysis suggestion'}</strong><small>${isDemo ? 'Text-only hint · confidence unavailable' : `${esc(result.provider)} · ${confidence} confidence`}</small></div></div><p>${esc(result.explanation)}</p>${result.category ? `<div class="analysis-suggestion"><span>Suggested</span><strong>${esc(result.categoryLabel)}</strong><strong>${esc(result.severity || 'Choose severity')}</strong><button type="button" id="applySuggestion">Apply suggestion</button></div>` : ''}<div class="analysis-disclaimer">${esc(result.disclaimer)}</div>`;
    $('#analysisResult').classList.remove('hidden');
    $('#applySuggestion')?.addEventListener('click', () => {
      $('#reportCategory').value = result.category || '';
      $('#reportSeverity').value = result.severity || '';
      if (!$('#reportTitle').value && result.categoryLabel) $('#reportTitle').value = `${result.categoryLabel} needs attention`;
      toast('Suggestion added. Check the category and severity before sending.', 'info');
    });
  }

  async function analyzePhoto() {
    if (!state.photo) return;
    if ($('#reportCategory').value === 'official_conduct') return toast('Photo analysis is disabled for confidential official-conduct concerns.', 'info');
    const button = $('#analyzeButton');
    setButtonBusy(button, true, 'Reviewing photo…');
    try {
      const form = new FormData();
      form.append('photo', state.photo);
      form.append('description', $('#reportDescription').value);
      renderAnalysis(await api('/ai/analyze', { method: 'POST', body: form }));
    } catch (error) { toast(`Analysis could not run: ${error.message}`, 'error'); }
    finally { setButtonBusy(button, false); }
  }

  function submissionKey() {
    return crypto.randomUUID ? crypto.randomUUID().replaceAll('-', '') : `${Date.now()}${Math.random().toString(36).slice(2, 18)}`;
  }

  function readReportFields() {
    return {
      category: $('#reportCategory').value,
      title: $('#reportTitle').value.trim(),
      description: $('#reportDescription').value.trim(),
      location: $('#reportLocation').value.trim(),
      ward: $('#reportWard').value.trim(),
      lat: $('#reportLat').value,
      lng: $('#reportLng').value,
      severity: $('#reportSeverity').value,
      reported_official_role: $('#reportedOfficialRole').value,
      reported_official_name: $('#reportedOfficialName').value.trim(),
      incident_at: $('#incidentAt').value,
      official_attest: $('#officialConductAttest').checked ? 'true' : '',
    };
  }

  function makeSubmissionForm(draft) {
    const form = new FormData();
    Object.entries(draft.fields).forEach(([key, value]) => form.append(key, String(value ?? '')));
    form.append('submission_key', draft.submissionKey);
    if (draft.analysisToken) form.append('ai_analysis_token', draft.analysisToken);
    if (draft.photo?.blob) form.append('photo', new File([draft.photo.blob], draft.photo.name || 'civic-photo.jpg', { type: draft.photo.type || 'image/jpeg' }));
    return form;
  }

  async function saveCurrentDraft(autoSubmit = false, key = submissionKey()) {
    if (!state.user) throw new Error('Sign in before saving a report draft.');
    if ($('#reportCategory').value === 'official_conduct') throw new Error('Confidential official-conduct reports cannot be stored offline. Reconnect before submitting one.');
    const draft = {
      id: key,
      submissionKey: key,
      ownerId: state.user.id,
      fields: readReportFields(),
      photo: state.photo ? { blob: state.photo, name: state.photo.name, type: state.photo.type } : null,
      createdAt: new Date().toISOString(),
      autoSubmit,
      syncing: false,
    };
    await saveOfflineDraftRecord(draft);
    return draft;
  }

  async function loadOfflineDrafts() {
    if (!state.user) { $('#offlineDraftList').innerHTML = '<p class="muted">Sign in to see drafts saved for your account on this browser.</p>'; return; }
    try {
      const drafts = (await listOfflineDrafts()).filter(draft => draft.ownerId === state.user.id).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
      $('#offlineDraftList').innerHTML = drafts.length ? drafts.map(draft => `<article class="offline-draft-card"><div><strong>${esc(draft.fields.title || 'Untitled report')}</strong><small>${esc(titleCase(draft.fields.category || 'Issue not selected'))} · saved ${fmtDate(draft.createdAt, { time: true })}${draft.photo ? ' · photo attached' : ''}${draft.autoSubmit ? ' · queued to submit' : ' · draft only'}</small></div><div class="offline-draft-actions"><button class="button button-line" data-resume-draft="${esc(draft.id)}">Open draft</button><button class="button button-line" data-delete-draft="${esc(draft.id)}" aria-label="Delete offline draft">Delete</button></div></article>`).join('') : '<p class="muted">No offline drafts saved on this device.</p>';
    } catch (error) { $('#offlineDraftList').innerHTML = `<p class="muted">${esc(error.message)}</p>`; }
  }

  async function resumeOfflineDraft(id) {
    const draft = (await listOfflineDrafts()).find(item => item.id === id && item.ownerId === state.user?.id);
    if (!draft) return toast('That draft is no longer available on this browser.', 'error');
    if (draft.fields.category === 'official_conduct') return toast('Private official-conduct reports are not stored offline.', 'error');
    openReport();
    for (const [field, inputId] of Object.entries({ category: 'reportCategory', title: 'reportTitle', description: 'reportDescription', location: 'reportLocation', ward: 'reportWard', lat: 'reportLat', lng: 'reportLng', severity: 'reportSeverity' })) byId(inputId).value = draft.fields[field] || '';
    $('#descriptionCount').textContent = `${draft.fields.description?.length || 0} / 2000`;
    state.photo = draft.photo?.blob ? new File([draft.photo.blob], draft.photo.name || 'civic-photo.jpg', { type: draft.photo.type || 'image/jpeg' }) : null;
    if (state.photo) {
      const preview = $('#photoPreview');
      if (preview.dataset.url) URL.revokeObjectURL(preview.dataset.url);
      preview.dataset.url = URL.createObjectURL(state.photo); preview.src = preview.dataset.url;
      preview.classList.remove('hidden'); $('#replacePhoto').classList.remove('hidden'); $('#analyzeButton').disabled = false;
    }
    toast(draft.autoSubmit ? 'This report is queued and will submit when your connection returns.' : 'Draft opened. Review it and submit when you are ready.', 'info');
  }

  async function syncQueuedDrafts() {
    if (syncingDrafts || !navigator.onLine || !state.user) return;
    syncingDrafts = true;
    try {
      const drafts = (await listOfflineDrafts()).filter(draft => draft.ownerId === state.user.id && draft.autoSubmit);
      for (const draft of drafts) {
        try {
          draft.syncing = true;
          await saveOfflineDraftRecord(draft);
          const result = await api('/reports', { method: 'POST', body: makeSubmissionForm(draft) });
          await deleteOfflineDraft(draft.id);
          const mailMessage = result.emailStatus === 'sent' ? ' Confirmation email sent.' : result.emailStatus === 'not_configured' ? ' Email receipts are not configured yet.' : '';
          toast(`Queued report accepted by CIVICLENS · ${result.report.id}.${mailMessage}`, result.emailStatus === 'failed' ? 'info' : 'success', 6500);
          loadHome();
        } catch (error) {
          draft.syncing = false;
          await saveOfflineDraftRecord(draft);
          if (!navigator.onLine || error instanceof TypeError) break;
          toast(`Queued report could not be sent: ${error.message}`, 'error', 6500);
          break;
        }
      }
    } catch (error) { toast(`Offline queue could not sync: ${error.message}`, 'error'); }
    finally { syncingDrafts = false; loadOfflineDrafts(); if (state.user) loadDashboard(); }
  }

  async function submitReport(event) {
    event.preventDefault();
    clearError($('#reportError'));
    if (!$('#reportForm').reportValidity()) return;
    const lat = Number($('#reportLat').value), lng = Number($('#reportLng').value);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) return showError($('#reportError'), 'Enter valid latitude and longitude, or choose a pin on the map.');
    if (!state.user) return openAuth('login', 'report');
    const fields = readReportFields();
    const sensitive = fields.category === 'official_conduct';
    if (sensitive && !navigator.onLine) return showError($('#reportError'), 'Confidential concerns cannot be stored offline. Reconnect before sending this report.');
    const key = submissionKey();
    const draft = { submissionKey: key, ownerId: state.user.id, fields, photo: state.photo ? { blob: state.photo, name: state.photo.name, type: state.photo.type } : null, analysisToken: navigator.onLine ? (state.analysis?.analysisToken || '') : '', createdAt: new Date().toISOString(), autoSubmit: true, syncing: false };
    if (!navigator.onLine) {
      try {
        await saveOfflineDraftRecord(draft);
        closeModal('reportModal'); toast('Report saved on this device and queued to submit when you reconnect.', 'info', 6500);
        setView('dashboard'); loadOfflineDrafts();
      } catch (error) { showError($('#reportError'), error.message); }
      return;
    }
    const button = $('#submitReport'); setButtonBusy(button, true, 'Submitting…');
    try {
      const result = await api('/reports', { method: 'POST', body: makeSubmissionForm(draft) });
      closeModal('reportModal');
      toast(`Report submitted · ${result.report.id}`);
      if (result.emailStatus === 'sent') toast('A CIVICLENS receipt was emailed to your account.', 'info');
      else if (result.emailStatus === 'not_configured') toast('Report saved. Email receipts are not configured on this server yet.', 'info', 6500);
      else if (result.emailStatus === 'failed') toast('Report saved, but the confirmation email could not be delivered. You can still track it in My reports.', 'info', 7000);
      if (result.potentialDuplicates?.length) toast(`${result.potentialDuplicates.length} nearby report${result.potentialDuplicates.length === 1 ? '' : 's'} may describe the same issue. City staff will review them.`, 'info', 7000);
      loadHome(); loadDashboard();
      setView('dashboard');
      await openReportDetail(result.report.id);
    } catch (error) {
      if (!sensitive && (error instanceof TypeError || !navigator.onLine)) {
        try {
          await saveOfflineDraftRecord(draft); closeModal('reportModal');
          toast('Connection lost. Your report was saved on this device and will submit when you reconnect.', 'info', 7000);
          setView('dashboard'); loadOfflineDrafts();
        } catch (saveError) { showError($('#reportError'), saveError.message); }
      } else showError($('#reportError'), error.message);
    }
    finally { setButtonBusy(button, false); }
  }

  function toggleOfficialReportFields() {
    const privateReport = $('#reportCategory').value === 'official_conduct';
    $('#officialConductFields').classList.toggle('hidden', !privateReport);
    $('#reportedOfficialRole').required = privateReport;
    $('#officialConductAttest').required = privateReport;
    $('#saveOfflineDraft').classList.toggle('hidden', privateReport);
    if (privateReport) {
      state.analysis = null;
      $('#analysisResult').classList.add('hidden');
      $('#analyzeButton').disabled = true;
    } else if (state.photo) $('#analyzeButton').disabled = false;
  }

  function timelineMarkup(report) {
    const timeline = report.timeline || [];
    return timeline.length ? `<ol class="timeline">${timeline.map((item, index) => `<li class="timeline-item ${index === timeline.length - 1 ? 'timeline-current' : ''}"><span class="timeline-marker"></span><div><strong>${esc(item.status)}</strong><p>${esc(item.detail || '')}</p><small>${fmtDate(item.createdAt, { time: true })}</small></div></li>`).join('')}</ol>` : '<p class="muted">Status history will appear here.</p>';
  }

  function impactMarkup(impact) {
    if (!impact) return '';
    return `<section class="detail-section impact-detail"><div class="detail-section-head"><div><span class="tiny-label">CIVICLENS IMPACTSCORE</span><h3>Priority context</h3></div><div class="impact-score"><strong>${impact.score}</strong><span>/ 100</span></div></div><p class="muted">A triage aid for municipal review. This score does not predict resolution or replace staff judgment.</p><div class="impact-factors">${impact.factors.map(factor => `<div class="impact-factor"><span>${esc(factor.name)}</span><div class="factor-track"><i style="width:${Math.max(0, factor.points / factor.maximum * 100)}%"></i></div><strong>${factor.points}<small>/${factor.maximum}</small></strong><small class="factor-explanation">${esc(factor.explanation)}</small></div>`).join('')}</div>${impact.isOverridden ? `<p class="override-note">Administrator override: ${impact.score}/100 · ${esc(impact.overrideReason || '')}</p>` : ''}</section>`;
  }

  function responseTargetMarkup(report) {
    const overdue = report.responseSlaStatus === 'overdue';
    const text = report.firstResponseAt
      ? `A CIVICLENS staff review update was recorded ${fmtDate(report.firstResponseAt, { time: true })}. This does not represent a government department decision.`
      : overdue
        ? `The 48-hour CIVICLENS initial review target has passed. Your report remains in the staff queue; this delay does not indicate a government department decision.`
        : `CIVICLENS targets an initial review update by ${fmtDate(report.firstResponseDueAt, { time: true })}. This is a service target, not a guarantee of government action.`;
    return `<p class="response-target ${overdue ? 'sla-overdue' : ''}" role="status"><strong>${report.firstResponseAt ? 'CIVICLENS review update' : overdue ? 'Review target overdue' : '48-hour CIVICLENS review target'}</strong><br>${esc(text)}</p>`;
  }

  function resolutionEvidenceMarkup(report) {
    const evidence = report.resolutionEvidence || [];
    if (!evidence.length && report.status !== 'Resolved') return '';
    const items = evidence.length ? `<div class="evidence-grid">${evidence.map(item => `<article class="evidence-item"><img data-resolution-evidence="${esc(item.id)}" alt="Staff completion photo for report ${esc(report.id)}" loading="lazy"><p>${esc(item.caption || 'Work completion evidence')}</p><small>${fmtDate(item.createdAt, { time: true })}</small></article>`).join('')}</div>` : '<p class="muted">Completion photo has not been added yet.</p>';
    return `<section class="resolution-evidence"><span class="tiny-label">COMPLETION EVIDENCE</span><h3>After photos from CIVICLENS staff</h3>${items}</section>`;
  }

  async function loadResolutionEvidencePhotos(report) {
    for (const item of report.resolutionEvidence || []) {
      const image = $(`[data-resolution-evidence="${CSS.escape(String(item.id))}"]`);
      if (!image) continue;
      try {
        const response = await fetch(`${API}/reports/${encodeURIComponent(report.id)}/resolution-evidence/${encodeURIComponent(item.id)}/photo`, { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} });
        if (!response.ok) throw new Error('Evidence unavailable');
        image.src = URL.createObjectURL(await response.blob());
      } catch { image.alt = 'Completion photo could not be loaded'; image.classList.add('hidden'); }
    }
  }

  function adminControls(report) {
    const statuses = (state.config?.statuses || []).map(status => `<option ${report.status === status ? 'selected' : ''}>${esc(status)}</option>`).join('');
    const severities = ['Low', 'Medium', 'High', 'Critical'].map(severity => `<option ${report.severity === severity ? 'selected' : ''}>${severity}</option>`).join('');
    const departments = ['Roads & Public Works', 'Sanitation', 'Stormwater & Drainage', 'Electrical Services', 'Parks & Horticulture', 'Municipal Operations', 'CIVICLENS Confidential Review'];
    return `<section class="detail-section admin-controls"><div class="detail-section-head"><div><span class="tiny-label">CITY OPERATIONS</span><h3>Manage report</h3></div>${badge('Administrator', 'status-verified')}</div>
      <div class="admin-control-grid"><label class="form-field"><span>Status</span><select id="detailStatus">${statuses}</select></label><label class="form-field"><span>Severity</span><select id="detailSeverity">${severities}</select></label>
      <label class="form-field"><span>Department</span><select id="detailDepartment"><option value="">Select team</option>${departments.map(dep => `<option ${report.department === dep ? 'selected' : ''}>${esc(dep)}</option>`).join('')}</select></label><label class="form-field"><span>Assigned to</span><input id="detailAssignee" maxlength="100" value="${esc(report.assignee || '')}" placeholder="Officer or team"></label>
      <label class="form-field field-full"><span>Resolution record</span><textarea id="detailResolution" rows="2" placeholder="Record completed work and verification details">${esc(report.resolutionDetails || '')}</textarea></label>
      <div class="override-grid"><label class="form-field"><span>ImpactScore override <small>(optional)</small></span><input id="impactOverride" type="number" min="0" max="100" placeholder="${report.impact.calculatedScore}"></label><label class="form-field"><span>Reason for override</span><input id="overrideReason" maxlength="500" value="${esc(report.impact.overrideReason || '')}" placeholder="Required when setting an override"></label></div>
      </div><button class="button button-primary button-full" id="saveAdminUpdate">Save municipal update</button>
      <div class="internal-note-form"><label class="form-field"><span>Internal note <small>(visible to staff only)</small></span><textarea id="adminNote" rows="2" maxlength="2000" placeholder="Add a coordination note"></textarea></label><button class="button button-line" id="saveAdminNote">Add note</button></div>
      ${report.internalNotes?.length ? `<div class="internal-notes"><h4>Staff notes</h4>${report.internalNotes.map(note => `<article><p>${esc(note.note)}</p><small>${esc(note.author)} · ${fmtDate(note.createdAt, { time: true })}</small></article>`).join('')}</div>` : ''}
      ${report.isSensitive ? `<div class="sensitive-report-banner"><strong>🔒 Confidential case</strong><br>${esc(report.reportedOfficialRole || 'Public official concern')}${report.reportedOfficialName ? ` · ${esc(report.reportedOfficialName)}` : ''}${report.incidentAt ? ` · Incident ${esc(report.incidentAt)}` : ''}. This report is hidden from the public map and community verification.</div>` : ''}
      ${report.status === 'Resolved' ? `<div class="resolution-evidence-form"><label class="form-field"><span>Staff completion photo</span><input id="resolutionEvidencePhoto" type="file" accept="image/jpeg,image/png,image/webp" required></label><label class="form-field"><span>Work completed</span><input id="resolutionEvidenceCaption" maxlength="500" placeholder="Describe the repair or follow-up"></label><button class="button button-line" id="uploadResolutionEvidence">Add completion evidence</button></div>` : ''}
      ${report.emailEvents?.length ? `<div class="detail-audit"><h4>Email delivery history</h4>${report.emailEvents.slice(0, 5).map(item => `<p><strong>${esc(titleCase(item.eventType))}</strong><small>${esc(item.deliveryStatus)} · ${fmtDate(item.createdAt, { time: true })}</small></p>`).join('')}</div>` : ''}
      ${!report.isSensitive ? `<div class="duplicate-review"><div class="duplicate-heading"><div><span class="tiny-label">DUPLICATE REVIEW</span><h4>Nearby matching reports</h4></div><button class="text-button" id="checkDuplicates">Check duplicates</button></div><div id="duplicateMatches"><p class="muted">Potential matches are suggestions. An administrator must confirm a merge.</p></div></div>` : ''}
      ${report.auditLog?.length ? `<div class="detail-audit"><h4>Recent audit trail</h4>${report.auditLog.slice(0, 6).map(item => `<p><strong>${esc(titleCase(item.action))}</strong><small>${fmtDate(item.createdAt, { time: true })}${item.actor ? ` · ${esc(item.actor)}` : ''}</small></p>`).join('')}</div>` : ''}
    </section>`;
  }

  async function openReportDetail(reportId) {
    state.detailId = reportId;
    $('#detailContent').innerHTML = '<div class="detail-loading"><span class="spinner"></span> Loading report…</div>';
    showModal('detailModal');
    try {
      const report = await api(`/reports/${encodeURIComponent(reportId)}`);
      state.detailReport = report;
      const admin = state.user?.role === 'admin';
      const isMine = state.mine.some(item => item.id === report.id) || report.isOwner;
      const showVerify = !!state.user && !admin && !isMine && !['Resolved', 'Rejected'].includes(report.status);
      const showReopen = !!state.user && !admin && isMine && report.status === 'Resolved';
      const showConfirmResolution = !!state.user && !admin && isMine && report.status === 'Resolved' && !report.resolutionConfirmedAt;
      const photo = report.photoAvailable && (admin || isMine) ? `<div class="detail-photo" id="detailPhoto">Loading private photo…</div>` : '';
      const sensitiveBanner = report.isSensitive ? `<div class="sensitive-report-banner"><strong>🔒 Confidential official-conduct concern</strong><br>This report is private to you and authorized CIVICLENS administrators. It is not shown on the public map or to community verifiers. Reports are reviewed as allegations, not findings.</div>` : '';
      const resolutionConfirmed = report.resolutionConfirmedAt ? `<p class="resolution-confirmed" role="status">You confirmed this resolution on ${fmtDate(report.resolutionConfirmedAt, { time: true })}.</p>` : '';
      $('#detailContent').innerHTML = `<div class="detail-header"><div class="detail-topline"><span class="eyebrow">${esc(report.id)}${report.isDemo ? ' · SAMPLE RECORD' : ''}</span><button class="copy-report-id" id="copyReportId">Copy ID</button></div><h2 id="detailTitle">${esc(report.title)}</h2><div class="detail-badges">${badge(report.categoryLabel, 'category-badge')}${badge(report.severity, severityClass(report.severity))}${badge(report.status, statusClass(report.status))}</div><p class="detail-location">⌖ ${esc(report.location)}${report.ward ? ` · ${esc(report.ward)}` : ''}</p><div class="detail-meta">Submitted ${fmtDate(report.createdAt, { time: true })} · ${esc(report.department || 'Municipal Operations')}</div></div>
        ${sensitiveBanner}${responseTargetMarkup(report)}${photo}<div class="detail-description"><span class="tiny-label">REPORT DETAILS</span><p>${esc(report.description)}</p>${report.isSensitive && report.reportedOfficialRole ? `<p><strong>Official type:</strong> ${esc(report.reportedOfficialRole)}${report.reportedOfficialName ? ` · ${esc(report.reportedOfficialName)}` : ''}${report.incidentAt ? ` · Incident ${esc(report.incidentAt)}` : ''}</p>` : ''}<div class="detail-coordinates">${Number(report.lat).toFixed(5)}, ${Number(report.lng).toFixed(5)} <a href="https://www.openstreetmap.org/?mlat=${encodeURIComponent(report.lat)}&mlon=${encodeURIComponent(report.lng)}#map=17/${encodeURIComponent(report.lat)}/${encodeURIComponent(report.lng)}" target="_blank" rel="noreferrer">Open in OpenStreetMap ↗</a></div></div>${resolutionEvidenceMarkup(report)}
        <div class="detail-columns"><section class="detail-section"><span class="tiny-label">STATUS HISTORY</span><h3>Report timeline</h3>${timelineMarkup(report)}${report.resolutionDetails ? `<div class="resolution-record"><strong>Resolution details</strong><p>${esc(report.resolutionDetails)}</p></div>` : ''}${resolutionConfirmed}${showConfirmResolution ? `<button class="button button-primary button-full" id="confirmResolution">✓ The issue is fixed</button>` : ''}${showReopen ? `<div class="reopen-request"><strong>Is this still unresolved?</strong><textarea id="reopenReason" rows="2" maxlength="1000" placeholder="Tell the city what still needs attention"></textarea><button class="button button-line button-full" id="reopenReport">Request another review</button></div>` : ''}</section><section class="detail-section detail-impact-section">${impactMarkup(report.impact)}${showVerify ? `<button class="button button-line button-full" id="verifyReport">✓ I can confirm this issue</button><p class="verification-count">${report.verificationCount || 0} community confirmations</p>` : `<p class="verification-count">${report.verificationCount || 0} community confirmations</p>`}</section></div>
        ${admin ? adminControls(report) : ''}
        <div class="detail-footer"><span>Reported by a community member · private account details stay private.</span><button class="button button-line" id="detailClose">Close</button></div>`;
      $('#detailClose').addEventListener('click', () => closeModal('detailModal'));
      $('#copyReportId').addEventListener('click', async () => { try { await navigator.clipboard.writeText(report.id); toast('Report ID copied.'); } catch { toast(report.id, 'info'); } });
      if (report.photoAvailable && (admin || isMine)) loadPrivatePhoto(report.id);
      if (report.resolutionEvidence?.length) loadResolutionEvidencePhotos(report);
      $('#verifyReport')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        setButtonBusy(button, true, 'Confirming…');
        try { await api(`/reports/${encodeURIComponent(report.id)}/verify`, { method: 'POST' }); toast('Thanks for confirming this community report.'); await openReportDetail(report.id); }
        catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
      });
      $('#reopenReport')?.addEventListener('click', async event => {
        const note = $('#reopenReason').value.trim();
        if (note.length < 2) return toast('Add a short note about why this needs another review.', 'info');
        const button = event.currentTarget;
        setButtonBusy(button, true, 'Sending…');
        try { await api(`/reports/${encodeURIComponent(report.id)}/reopen`, { method: 'POST', body: { note } }); toast('Your request for another review has been sent.'); await loadDashboard(); await openReportDetail(report.id); }
        catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
      });
      $('#confirmResolution')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        setButtonBusy(button, true, 'Saving confirmation…');
        try { await api(`/reports/${encodeURIComponent(report.id)}/confirm-resolution`, { method: 'POST' }); toast('Thanks for confirming the issue is fixed.'); await loadDashboard(); await openReportDetail(report.id); }
        catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
      });
      if (admin) bindAdminDetail(report);
    } catch (error) {
      $('#detailContent').innerHTML = `<div class="empty-state"><h2>Report unavailable</h2><p>${esc(error.message)}</p><button class="button button-line" id="detailClose">Close</button></div>`;
      $('#detailClose').addEventListener('click', () => closeModal('detailModal'));
    }
  }

  async function loadPrivatePhoto(reportId) {
    try {
      const response = await fetch(`${API}/reports/${encodeURIComponent(reportId)}/photo`, { headers: { Authorization: `Bearer ${state.token}` } });
      if (!response.ok) throw new Error('Private photo is unavailable.');
      const url = URL.createObjectURL(await response.blob());
      $('#detailPhoto').innerHTML = `<img src="${url}" alt="Photo attached to civic report">`;
    } catch { if ($('#detailPhoto')) $('#detailPhoto').textContent = 'Photo could not be loaded.'; }
  }

  function bindAdminDetail(report) {
    $('#saveAdminUpdate').addEventListener('click', async event => {
      const button = event.currentTarget;
      const overrideInput = $('#impactOverride').value;
      const payload = {
        status: $('#detailStatus').value, severity: $('#detailSeverity').value,
        department: $('#detailDepartment').value || report.department,
        assignee: $('#detailAssignee').value.trim(), resolution_details: $('#detailResolution').value.trim(),
      };
      if (overrideInput !== '') {
        payload.impact_override = Number(overrideInput);
        payload.override_reason = $('#overrideReason').value.trim();
      }
      setButtonBusy(button, true, 'Saving…');
      try {
        const result = await api(`/admin/reports/${encodeURIComponent(report.id)}`, { method: 'PATCH', body: payload });
        toast('CIVICLENS review update saved with an audit record.');
        if (result.emailStatus === 'sent') toast('A status update email was sent to the reporter.', 'info');
        else if (result.emailStatus === 'not_configured') toast('Email receipts are not configured on this server.', 'info');
        await loadAdmin(); await openReportDetail(report.id);
      }
      catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
    });
    $('#uploadResolutionEvidence')?.addEventListener('click', async event => {
      const button = event.currentTarget;
      const photo = $('#resolutionEvidencePhoto').files?.[0];
      if (!photo) return toast('Choose a completion photo first.', 'info');
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(photo.type) || photo.size > 6 * 1024 * 1024) return toast('Choose a JPG, PNG or WebP image up to 6 MB.', 'error');
      const form = new FormData(); form.append('photo', photo); form.append('caption', $('#resolutionEvidenceCaption').value.trim());
      setButtonBusy(button, true, 'Uploading…');
      try { await api(`/admin/reports/${encodeURIComponent(report.id)}/resolution-evidence`, { method: 'POST', body: form }); toast('Completion evidence added.'); await openReportDetail(report.id); }
      catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
    });
    $('#saveAdminNote').addEventListener('click', async event => {
      const button = event.currentTarget;
      const note = $('#adminNote').value.trim();
      if (!note) return toast('Write a note before adding it.', 'info');
      setButtonBusy(button, true, 'Adding…');
      try { await api(`/admin/reports/${encodeURIComponent(report.id)}/notes`, { method: 'POST', body: { note } }); toast('Internal note added.'); await openReportDetail(report.id); }
      catch (error) { toast(error.message, 'error'); setButtonBusy(button, false); }
    });
    $('#checkDuplicates').addEventListener('click', async event => {
      const button = event.currentTarget;
      setButtonBusy(button, true, 'Checking…');
      try {
        const matches = await api(`/duplicates?report_id=${encodeURIComponent(report.id)}`);
        $('#duplicateMatches').innerHTML = matches.length ? matches.map(match => `<article class="duplicate-card"><div><strong>${esc(match.title)}</strong><small>${esc(match.reportId)} · ${match.distanceMeters} m · ${match.similarityScore}% match${match.imageSimilarityPercent !== null ? ` · ${match.imageSimilarityPercent}% image similarity` : ''}</small><small>${esc(match.location)} · ${esc(match.status)}</small></div><button class="button button-line" data-merge-into="${esc(match.reportId)}">Merge into this report</button></article>`).join('') : '<p class="muted">No potential duplicates found nearby.</p>';
        $$('[data-merge-into]', $('#duplicateMatches')).forEach(button => button.addEventListener('click', async () => {
          if (!window.confirm(`Merge ${report.id} into ${button.dataset.mergeInto}? Both report histories will remain available.`)) return;
          try { await api(`/admin/reports/${encodeURIComponent(report.id)}/merge`, { method: 'POST', body: { canonical_report_id: button.dataset.mergeInto } }); toast('Duplicate linked. Original report history is retained.'); await loadAdmin(); await openReportDetail(report.id); }
          catch (error) { toast(error.message, 'error'); }
        }));
      } catch (error) { toast(error.message, 'error'); }
      finally { setButtonBusy(button, false); }
    });
  }

  async function exportCsv() {
    const button = $('#adminExport'); setButtonBusy(button, true, 'Preparing export…');
    try {
      const response = await fetch(`${API}/admin/export.csv`, { headers: { Authorization: `Bearer ${state.token}` } });
      if (!response.ok) { const body = await response.json(); throw new Error(body.detail || 'Export failed.'); }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob); const link = document.createElement('a');
      link.href = url; link.download = 'civiclens-reports.csv'; link.click(); URL.revokeObjectURL(url);
      toast('CSV export downloaded.');
    } catch (error) { toast(error.message, 'error'); }
    finally { setButtonBusy(button, false); }
  }

  async function demoLogin(role) {
    const credentials = role === 'admin'
      ? { email: 'admin@civiclens.demo', password: 'AdminDemo#2026' }
      : { email: 'citizen@civiclens.demo', password: 'CitizenDemo#2026' };
    try { const session = await api('/auth/login', { method: 'POST', body: credentials }); setSession(session); }
    catch (error) { toast(`Demo sign-in unavailable: ${error.message}`, 'error'); }
  }

  async function handleAuthSubmit(event) {
    event.preventDefault();
    clearError($('#authError'));
    const register = $('#registerTab').classList.contains('selected');
    const body = { email: $('#authEmail').value.trim(), password: $('#authPassword').value };
    if (register) body.name = $('#authName').value.trim();
    const button = $('#authSubmit'); setButtonBusy(button, true, register ? 'Creating…' : 'Signing in…');
    try { setSession(await api(register ? '/auth/register' : '/auth/login', { method: 'POST', body })); }
    catch (error) { showError($('#authError'), error.message); }
    finally { setButtonBusy(button, false); }
  }

  async function saveProfile(event) {
    event.preventDefault(); clearError($('#profileError'));
    const body = { name: $('#profileEditName').value.trim() };
    if ($('#profileEditPassword').value) body.password = $('#profileEditPassword').value;
    const button = $('button[type="submit"]', $('#profileForm')); setButtonBusy(button, true, 'Saving…');
    try {
      state.user = await api('/profile', { method: 'PATCH', body }); updateAccountUI();
      $('#profileEditPassword').value = ''; closeModal('profileModal'); toast('Profile updated.');
    } catch (error) { showError($('#profileError'), error.message); }
    finally { setButtonBusy(button, false); }
  }

  function openProfile() {
    if (!state.user) return openAuth();
    $('#profileEditName').value = state.user.name;
    $('#profileEditPassword').value = '';
    clearError($('#profileError'));
    showModal('profileModal');
  }

  function handleReportPhoto(event) {
    const file = event.target.files[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 6 * 1024 * 1024) {
      event.target.value = ''; state.photo = null; $('#analyzeButton').disabled = true;
      return toast('Choose a JPG, PNG or WebP photo up to 6 MB.', 'error');
    }
    state.photo = file;
    state.analysis = null;
    $('#analysisResult').classList.add('hidden');
    const preview = $('#photoPreview');
    if (preview.dataset.url) URL.revokeObjectURL(preview.dataset.url);
    preview.dataset.url = URL.createObjectURL(file); preview.src = preview.dataset.url;
    preview.classList.remove('hidden'); $('#replacePhoto').classList.remove('hidden'); $('#analyzeButton').disabled = $('#reportCategory').value === 'official_conduct'; $('#analysisResult').classList.add('hidden'); state.analysis = null;
  }

  function handleEvent(event) {
    const viewButton = event.target.closest('[data-view-link]');
    if (viewButton) { event.preventDefault(); setView(viewButton.dataset.viewLink); return; }
    const reportAction = event.target.closest('[data-action="report"],#headerReport');
    if (reportAction) { openReport(); return; }
    const impactAction = event.target.closest('[data-action="impact-info"]');
    if (impactAction) { openImpactInfo(); return; }
    const demoButton = event.target.closest('[data-demo-login]');
    if (demoButton) { demoLogin(demoButton.dataset.demoLogin); return; }
    const reportCardClick = event.target.closest('[data-open-report]');
    if (reportCardClick) { openReportDetail(reportCardClick.dataset.openReport); return; }
    const categoryCard = event.target.closest('[data-category]');
    if (categoryCard) { setView('map'); $('#mapCategory').value = categoryCard.dataset.category; loadMapView(); return; }
    const popup = event.target.closest('[data-popup-report]');
    if (popup) { openReportDetail(popup.dataset.popupReport); return; }
    const resumeDraft = event.target.closest('[data-resume-draft]');
    if (resumeDraft) { resumeOfflineDraft(resumeDraft.dataset.resumeDraft); return; }
    const deleteDraft = event.target.closest('[data-delete-draft]');
    if (deleteDraft) {
      deleteOfflineDraft(deleteDraft.dataset.deleteDraft).then(() => { toast('Offline draft deleted.', 'info'); loadOfflineDrafts(); }).catch(error => toast(error.message, 'error'));
    }
  }

  function updateConnectionNotice() {
    const notice = $('#offlineNotice');
    notice.classList.toggle('hidden', navigator.onLine);
    notice.textContent = navigator.onLine
      ? 'Connection restored. CIVICLENS is sending any queued report drafts.'
      : 'You are offline. Regular reports can be saved on this device and sent when you reconnect. Confidential official-conduct reports are not saved offline.';
  }

  function bindEvents() {
    document.addEventListener('civiclens:language', event => { state.language = event.detail.language === 'or' ? 'or-IN' : `${event.detail.language}-IN`; });
    window.CIVICLENS_LANGUAGE.init();
    state.language = window.CIVICLENS_LANGUAGE.get() === 'or' ? 'or-IN' : `${window.CIVICLENS_LANGUAGE.get()}-IN`;
    document.addEventListener('click', handleEvent);
    $('#mobileMenu').addEventListener('click', () => {
      const opened = $('#mainNav').classList.toggle('mobile-open');
      $('#mobileMenu').setAttribute('aria-expanded', String(opened));
    });
    $('#signInButton').addEventListener('click', () => openAuth());
    $('#aadhaarLoginButton').addEventListener('click', () => $('#aadhaarStatusMessage').classList.toggle('hidden'));
    $('#dashboardSignIn').addEventListener('click', () => openAuth());
    $('#adminSignIn').addEventListener('click', () => openAuth());
    $('#loginTab').addEventListener('click', () => setAuthMode('login'));
    $('#registerTab').addEventListener('click', () => setAuthMode('register'));
    $('#authForm').addEventListener('submit', handleAuthSubmit);
    $('#profileForm').addEventListener('submit', saveProfile);
    $('#profileButton').addEventListener('click', openProfile);
    $('#editProfileQuick').addEventListener('click', openProfile);
    $('#signOutButton').addEventListener('click', () => {
      state.token = ''; state.user = null; state.mine = []; sessionStorage.removeItem('civiclens_token'); updateAccountUI();
      $('#accountMenu').classList.add('hidden'); toast('You have signed out.'); setView('home');
    });
    $('#accountButton').addEventListener('click', () => $('#accountMenu').classList.toggle('hidden'));
    $('#dismissRibbon').addEventListener('click', () => $('#demoRibbon').classList.add('hidden'));
    $$('[data-close-modal]').forEach(button => button.addEventListener('click', () => closeModal(button.closest('.modal-backdrop').id)));
    $$('.modal-backdrop').forEach(backdrop => backdrop.addEventListener('click', event => { if (event.target === backdrop) closeModal(backdrop.id); }));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') closeTopModal();
      const activeBackdrop = $('.modal-backdrop:not(.hidden)');
      if (event.key === 'Tab' && activeBackdrop) {
        const focusable = $$('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])', activeBackdrop).filter(node => node.offsetParent !== null);
        if (focusable.length) {
          const first = focusable[0], last = focusable[focusable.length - 1];
          const activeIndex = focusable.indexOf(document.activeElement);
          if (event.shiftKey && (activeIndex === 0 || activeIndex === -1)) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && (activeIndex === focusable.length - 1 || activeIndex === -1)) { event.preventDefault(); first.focus(); }
        }
      }
      if (event.key === 'Enter' && event.target.matches('[data-open-report]')) openReportDetail(event.target.dataset.openReport);
    });
    ['mapCategory', 'mapSeverity', 'mapStatus', 'mapFrom', 'mapTo', 'mapOpenOnly'].forEach(id => byId(id).addEventListener('change', loadMapView));
    $('#resetFilters').addEventListener('click', () => { ['mapCategory', 'mapSeverity', 'mapStatus', 'mapFrom', 'mapTo', 'mapSearch'].forEach(id => byId(id).value = ''); $('#mapOpenOnly').checked = false; loadMapView(); });
    $('#searchLocation').addEventListener('click', searchMapLocation);
    $('#mapSearch').addEventListener('keydown', event => { if (event.key === 'Enter') searchMapLocation(); });
    $('#mapMode').addEventListener('click', () => { $('#mapFrame').classList.remove('hidden'); $('#mapList').classList.add('hidden'); $('#mapMode').classList.add('selected'); $('#listMode').classList.remove('selected'); if (state.map) state.map.invalidateSize(); });
    $('#listMode').addEventListener('click', () => { $('#mapFrame').classList.add('hidden'); $('#mapList').classList.remove('hidden'); $('#listMode').classList.add('selected'); $('#mapMode').classList.remove('selected'); });
    $('#mapSearch').addEventListener('input', () => {
      window.clearTimeout(state.searchTimer);
      const query = $('#mapSearch').value.trim();
      if (!query) return loadMapView();
      state.searchTimer = window.setTimeout(async () => {
        try {
          const reports = await api(`/reports?${mapQuery(query)}`);
          $('#mapList').innerHTML = reports.length ? reports.map(report => reportCard(report)).join('') : '<div class="empty-state">No reports match that search.</div>';
          renderMapMarkers(reports); $('#mapResultsCount').textContent = `${reports.length} matching reports`;
        } catch { /* Search errors are surfaced when the user requests a map location. */ }
      }, 320);
    });
    $('#refreshMine').addEventListener('click', loadDashboard);
    $('#saveOfflineDraft').addEventListener('click', async () => {
      try { await saveCurrentDraft(false); toast('Draft saved on this device. Open My reports to resume it.', 'success'); closeModal('reportModal'); setView('dashboard'); loadOfflineDrafts(); }
      catch (error) { showError($('#reportError'), error.message); }
    });
    $('#reportCategory').addEventListener('change', toggleOfficialReportFields);
    $('#adminSearch').addEventListener('input', renderAdminRows);
    $('#adminStatus').addEventListener('change', renderAdminRows);
    $('#adminSeverity').addEventListener('change', renderAdminRows);
    $('#adminReportRows').addEventListener('click', event => { const row = event.target.closest('[data-admin-report]'); if (row) openReportDetail(row.dataset.adminReport); });
    $('#adminReportRows').addEventListener('keydown', event => { if (event.key === 'Enter') event.target.closest('[data-admin-report]')?.click(); });
    $$('.admin-tab').forEach(button => button.addEventListener('click', () => selectAdminTab(button.dataset.adminTab)));
    $('#adminExport').addEventListener('click', exportCsv);
    $('#refreshAdmin').addEventListener('click', loadAdmin);
    $('#reportPhoto').addEventListener('change', handleReportPhoto);
    $('#analyzeButton').addEventListener('click', analyzePhoto);
    $('#reportForm').addEventListener('submit', submitReport);
    window.addEventListener('online', () => { updateConnectionNotice(); syncQueuedDrafts(); });
    window.addEventListener('offline', updateConnectionNotice);
    $('#locateMe').addEventListener('click', useMyLocation);
    $('#reportDescription').addEventListener('input', () => {
      $('#descriptionCount').textContent = `${$('#reportDescription').value.length} / 2000`;
      if (state.analysis) { state.analysis = null; $('#analysisResult').classList.add('hidden'); }
    });
    ['reportLat', 'reportLng'].forEach(id => byId(id).addEventListener('change', () => {
      const lat = Number($('#reportLat').value), lng = Number($('#reportLng').value);
      if (Number.isFinite(lat) && Number.isFinite(lng) && state.pickMap) { state.pickMap.setView([lat, lng], 16); setPickCoordinates(lat, lng); }
    }));
    $('#footerYear').textContent = new Date().getFullYear();
  }

  async function init() {
    bindEvents();
    updateConnectionNotice();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => toast('Offline app shell could not be prepared. You can still use CIVICLENS while connected.', 'info', 6000));
    await loadConfig();
    await loadIdentity();
    await loadHome();
    loadOfflineDrafts();
    if (navigator.onLine) syncQueuedDrafts();
    const requested = location.hash.replace('#', '');
    if (['map', 'dashboard', 'admin'].includes(requested)) setView(requested, false);
  }
  init();
})();
