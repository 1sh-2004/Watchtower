/* Sentinel dashboard.
 * All dynamic content is inserted with textContent / createTextNode, never
 * innerHTML, so process names, service names or alert text cannot inject markup. */

// ---------- helpers ----------
function h(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (k === 'class') node.className = v;
        else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
        if (c === null || c === undefined || c === false) continue;
        node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function replaceChildren(id, ...nodes) {
    const el = document.getElementById(id);
    if (el) el.replaceChildren(...nodes.flat());
}

async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
}

function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400), hr = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
    const parts = [];
    if (d > 0) parts.push(`${d}d`);
    if (hr > 0) parts.push(`${hr}h`);
    if (m > 0) parts.push(`${m}m`);
    return parts.join(' ') || '< 1m';
}

function formatRate(bytesPerSec) {
    if (bytesPerSec === null || bytesPerSec === undefined) return '--';
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    let v = bytesPerSec, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function timeLabel(ts, hours) {
    const d = new Date(ts * 1000);
    return hours > 24 ? d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function ago(ts) {
    if (!ts) return '--';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}

const badge = (text, kind) => h('span', { class: `badge ${kind}` }, text);
const SEV = { critical: 'bad', warning: 'warn', info: 'info' };

// ---------- tabs ----------
let activeTab = 'overview';
function initTabs() {
    const tabs = document.querySelectorAll('.tab');
    tabs.forEach(t => t.addEventListener('click', () => {
        tabs.forEach(x => x.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        t.classList.add('active');
        activeTab = t.dataset.tab;
        document.getElementById(`tab-${activeTab}`).classList.add('active');
        refreshActive();
    }));
}

// ---------- overview ----------
async function fetchCurrent() {
    const d = await getJSON('/api/system/current');
    setText('cpu-percent', `${d.cpu.percent}%`);
    setText('cpu-cores', `${d.cpu.count} cores`);
    setText('ram-percent', `${d.ram.percent}%`);
    setText('ram-usage', `${d.ram.used_gb} / ${d.ram.total_gb} GB`);
    setText('disk-percent', `${d.disk.percent}%`);
    setText('disk-usage', `${d.disk.used_gb} / ${d.disk.total_gb} GB`);
    setText('uptime', `Uptime: ${formatUptime(d.uptime)}`);
    setText('disk-io', d.disk.io_supported ? `${formatRate(d.disk.read_bps)} / ${formatRate(d.disk.write_bps)}` : 'n/a');
    setText('net-io', `${formatRate(d.network.down_bps)} / ${formatRate(d.network.up_bps)}`);
    if (d.gpus && d.gpus.length) {
        const g = d.gpus[0];
        setText('gpu-util', g.util_percent === null ? '--' : `${g.util_percent}%`);
        setText('gpu-detail', `${g.name}${g.temp_c !== null ? ' • ' + g.temp_c + '°C' : ''}`);
    } else {
        setText('gpu-util', 'n/a');
        setText('gpu-detail', 'No NVIDIA GPU detected');
    }
}

async function fetchProcesses() {
    const d = await getJSON('/api/system/processes');
    setText('process-count', d.total_count);
    const rows = d.processes.map(p => h('tr', {}, h('td', {}, p.pid), h('td', {}, p.name), h('td', {}, `${p.cpu}%`), h('td', {}, `${p.memory}%`)));
    replaceChildren('processes-body', rows.length ? rows : [h('tr', {}, h('td', { colspan: 4 }, 'No busy processes'))]);
}

let currentHours = 1, metricsChart = null, ioChart = null;

function lineChart(canvasId, datasets, yOpts) {
    return new Chart(document.getElementById(canvasId).getContext('2d'), {
        type: 'line',
        data: { labels: [], datasets },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            plugins: { legend: { position: 'top' }, tooltip: { mode: 'index', intersect: false } },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: { y: yOpts },
            elements: { point: { radius: 0 } },
        },
    });
}

const ds = (label, color) => ({ label, data: [], borderColor: color, backgroundColor: color + '1a', tension: 0.3, fill: true, spanGaps: true });

async function fetchHistory() {
    if (typeof Chart === 'undefined') return;  // degrade gracefully: cards and tables still work
    const d = await getJSON(`/api/system/history?hours=${currentHours}`);
    const labels = d.timestamps.map(t => timeLabel(t, currentHours));

    if (!metricsChart) {
        metricsChart = lineChart('metricsChart', [ds('CPU %', '#667eea'), ds('RAM %', '#d946ef'), ds('Disk %', '#0ea5e9')],
            { beginAtZero: true, max: 100, ticks: { callback: v => v + '%' } });
    }
    metricsChart.data.labels = labels;
    metricsChart.data.datasets[0].data = d.cpu;
    metricsChart.data.datasets[1].data = d.ram;
    metricsChart.data.datasets[2].data = d.disk;
    metricsChart.update('none');

    if (!ioChart) {
        ioChart = lineChart('ioChart',
            [ds('Disk read', '#16a34a'), ds('Disk write', '#f59e0b'), ds('Net down', '#667eea'), ds('Net up', '#ef4444')],
            { beginAtZero: true, ticks: { callback: v => formatRate(v) } });
        ioChart.data.datasets.forEach(x => { x.fill = false; });
    }
    ioChart.data.labels = labels;
    ioChart.data.datasets[0].data = d.disk_read_bps;
    ioChart.data.datasets[1].data = d.disk_write_bps;
    ioChart.data.datasets[2].data = d.net_down_bps;
    ioChart.data.datasets[3].data = d.net_up_bps;
    ioChart.update('none');
}

function initTimeControls() {
    const buttons = document.querySelectorAll('.time-btn');
    buttons.forEach(b => b.addEventListener('click', () => {
        buttons.forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        currentHours = parseInt(b.dataset.hours, 10);
        fetchHistory().catch(console.error);
    }));
}

// ---------- alerts ----------
async function fetchOpenAlerts() {
    const { alerts } = await getJSON('/api/alerts?status=open&limit=20');
    const order = { critical: 0, warning: 1, info: 2 };
    alerts.sort((a, b) => order[a.severity] - order[b.severity]);
    replaceChildren('alerts-container', alerts.map(a =>
        h('div', { class: `alert ${a.severity}` }, h('strong', {}, a.severity.toUpperCase()), ' ', a.message,
            h('span', { class: 'alert-age' }, ago(a.first_ts)))));
}

async function fetchAlertHistory() {
    const { alerts } = await getJSON('/api/alerts?limit=100');
    const rows = alerts.map(a => h('tr', {},
        h('td', {}, badge(a.status, a.status === 'open' ? 'bad' : 'ok')),
        h('td', {}, badge(a.severity, SEV[a.severity] || 'info')),
        h('td', {}, a.source), h('td', {}, a.message),
        h('td', {}, ago(a.first_ts)), h('td', {}, ago(a.last_ts)), h('td', {}, a.count)));
    replaceChildren('alert-history-body', rows.length ? rows : [h('tr', {}, h('td', { colspan: 7 }, 'No alerts recorded yet'))]);
}

// ---------- services ----------
function keyCell(latest, svc) {
    const d = latest ? latest.key_days_left : null;
    if (d !== null && d !== undefined) {
        if (d < 0) return badge(`expired ${-d}d ago`, 'bad');
        return badge(`${d}d left`, d <= 3 ? 'bad' : d <= 14 ? 'warn' : 'ok');
    }
    if (svc && svc.key_set === true) return badge('key set', 'ok');
    if (svc && svc.key_set === false) return badge('no key', 'warn');
    return '--';
}

async function fetchServices() {
    const { services } = await getJSON('/api/services');
    if (!services.length) {
        replaceChildren('services-body', h('tr', {}, h('td', { colspan: 8 },
            'No services yet. Add one in the Settings tab (or in sentinel.yaml).')));
        return;
    }
    const rows = services.map(s => {
        const l = s.latest, st = s.stats_24h;
        let status = badge('pending', 'info');
        if (l) status = l.ok ? badge('up', 'ok') : badge(l.error || 'down', 'bad');
        const slow = l && l.latency_ms > s.latency_warn_ms;
        const quota = l && l.rl_remaining !== null && l.rl_remaining !== undefined
            ? `${l.rl_remaining}${l.rl_limit ? ' / ' + l.rl_limit : ''}` : '--';
        return h('tr', {},
            h('td', {}, h('strong', {}, s.name), h('div', { class: 'sub' }, s.host || '')),
            h('td', {}, status),
            h('td', {}, l && l.latency_ms !== null ? h('span', { class: slow ? 'text-warn' : '' }, `${l.latency_ms} ms`) : '--'),
            h('td', {}, st.uptime_percent === null ? '--' : `${st.uptime_percent}%`),
            h('td', {}, st.p95_latency_ms === null ? '--' : `${st.p95_latency_ms} ms`),
            h('td', {}, `${st.checks} / ${st.failures}`),
            h('td', {}, quota), h('td', {}, keyCell(l, s)));
    });
    replaceChildren('services-body', rows);
}

// ---------- cloud ----------
function panel(title, ...body) { return h('section', { class: 'panel' }, h('h2', {}, title), ...body); }
function simpleTable(headers, rows, empty) {
    return h('div', { class: 'table-container' }, h('table', {},
        h('thead', {}, h('tr', {}, headers.map(x => h('th', {}, x)))),
        h('tbody', {}, rows.length ? rows : [h('tr', {}, h('td', { colspan: headers.length }, empty))])));
}

function awsHints(errors) {
    const all = errors.join(' ');
    const hints = [];
    if (/NoCredentialsError|PartialCredentialsError/.test(all))
        hints.push('No AWS credentials were found. Enter an access key in Settings, or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, or an AWS profile.');
    if (/AuthFailure|InvalidClientTokenId|SignatureDoesNotMatch|ExpiredToken|InvalidToken/.test(all))
        hints.push('AWS rejected the keys (wrong, deleted, or expired). Re-enter them in Settings and use Test connection.');
    if (/UnauthorizedOperation|AccessDenied|UnauthorizedAccess|OptInRequired/.test(all))
        hints.push('The keys work but lack a permission. Attach infra/aws-readonly-policy.json to the IAM user. For "cost" errors also check that Cost Explorer is enabled in the AWS console and that the user may view billing.');
    if (/EndpointConnectionError|ConnectTimeout|ConnectionError/.test(all))
        hints.push('Could not reach AWS. Check this machine\'s internet connection or proxy.');
    return hints;
}

async function fetchCloud() {
    const d = await getJSON('/api/cloud/aws');
    if (!d.enabled) {
        replaceChildren('cloud-content', panel('AWS monitoring is off',
            h('p', { class: 'hint' }, 'Nothing is being collected from AWS, and no AWS calls are made. To turn it on, open the Settings tab, ' +
                'enter read-only AWS keys and your regions (or set aws.enabled: true in sentinel.yaml). ' +
                'Use the policy in infra/aws-readonly-policy.json. Sentinel never starts, stops or deletes resources.')));
        return;
    }
    if (d.pending) { replaceChildren('cloud-content', panel('AWS', h('p', {}, 'First collection in progress...'))); return; }

    const out = [];
    if (d.errors && d.errors.length) out.push(panel('Collection issues',
        h('ul', { class: 'plain-list' }, d.errors.map(e => h('li', {}, e))),
        ...awsHints(d.errors).map(t => h('p', { class: 'hint' }, t))));

    const c = d.cost;
    if (c) {
        const pct = c.budget_used_percent;
        out.push(panel('Spend this month',
            h('div', { class: 'cost-row' },
                h('div', {}, h('div', { class: 'stat-value' }, `$${c.month_to_date_usd}`), h('div', { class: 'stat-detail' }, 'month to date')),
                h('div', {}, h('div', { class: 'stat-value' }, c.forecast_usd === null ? '--' : `$${c.forecast_usd}`), h('div', { class: 'stat-detail' }, 'forecast for the month')),
                h('div', {}, h('div', { class: 'stat-value' }, c.budget_usd ? `$${c.budget_usd}` : 'none'), h('div', { class: 'stat-detail' }, 'budget'))),
            pct !== null && pct !== undefined ? h('div', { class: 'bar' }, h('div', { class: `bar-fill ${pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : 'ok'}`, style: `width:${Math.min(100, pct)}%` })) : null,
            simpleTable(['Top services', 'Spend'], c.top_services.map(s => h('tr', {}, h('td', {}, s.service), h('td', {}, `$${s.usd}`))), 'No cost data')));
    }

    out.push(panel('EC2 instances', simpleTable(['ID', 'Name', 'Type', 'Region', 'State', `Avg CPU`],
        d.instances.map(i => h('tr', {}, h('td', {}, i.id), h('td', {}, i.name || '--'), h('td', {}, i.type || '--'), h('td', {}, i.region),
            h('td', {}, badge(i.state, i.state === 'running' ? 'ok' : 'info')), h('td', {}, i.avg_cpu_percent === null ? '--' : `${i.avg_cpu_percent}%`))),
        'No instances found')));

    out.push(panel('S3 storage', simpleTable(['Bucket', 'Size (Standard)', 'Objects'],
        d.buckets.map(b => h('tr', {}, h('td', {}, b.name), h('td', {}, b.size_gb === null ? '--' : `${b.size_gb} GB`), h('td', {}, b.objects === null ? '--' : b.objects))),
        'No buckets found')));

    out.push(panel('Findings: unused or unexpected resources', simpleTable(['Severity', 'Finding'],
        d.findings.map(f => h('tr', {}, h('td', {}, badge(f.severity, SEV[f.severity])), h('td', {}, f.message))),
        'Nothing wasteful or unexpected found')));

    out.push(h('p', { class: 'hint' }, `Last collected ${ago(d.collected_at)}. Cost data refreshes every few hours (Cost Explorer charges per request).`));
    replaceChildren('cloud-content', out);
}

// ---------- SLOs ----------
function budgetBar(pct) {
    const shown = Math.max(0, Math.min(100, pct));
    return h('div', { class: 'bar' }, h('div', { class: `bar-fill ${pct <= 0 ? 'bad' : pct < 25 ? 'warn' : 'ok'}`, style: `width:${shown}%` }));
}

function burnText(b) {
    return b.long_rate === null ? '--' : `${b.long_rate}x / ${b.short_rate === null ? '--' : b.short_rate + 'x'}`;
}

async function fetchSlo() {
    const { slos } = await getJSON('/api/slo');
    if (!slos.length) {
        replaceChildren('slo-content', panel('No SLOs defined',
            h('p', { class: 'hint' }, 'Set an availability goal on a service in the Settings tab, or add an slo block in sentinel.yaml, for example slo: { target: 99.9, window_days: 7 }.')));
        return;
    }
    const kind = { ok: 'ok', slow_burn: 'warn', fast_burn: 'bad', exhausted: 'bad' };
    const cards = slos.map(r => panel(`${r.service}: ${r.target_percent}% over ${r.window_days} day(s)`,
        h('div', { class: 'cost-row' },
            h('div', {}, h('div', { class: 'stat-value' }, r.sli_percent === null ? '--' : `${r.sli_percent}%`), h('div', { class: 'stat-detail' }, 'actual availability')),
            h('div', {}, h('div', { class: 'stat-value' }, `${r.budget_remaining_percent}%`), h('div', { class: 'stat-detail' }, 'error budget left')),
            h('div', {}, badge(r.status.replace('_', ' '), kind[r.status] || 'info'))),
        budgetBar(r.budget_remaining_percent),
        h('p', { class: 'hint' },
            `${r.bad_checks} failed of ${r.checks} checks; the budget allows about ${r.budget_total_checks} over the window. ` +
            `Window ${r.coverage_percent}% filled with data.` +
            (r.exhausts_in_hours !== null ? ` At the current pace the budget runs out in about ${r.exhausts_in_hours} h.` : '')),
        simpleTable(['Alert', 'Burn rate (long / short window)', 'Alerts at', 'State'], ['fast', 'slow'].map(k => {
            const b = r.burn[k];
            return h('tr', {}, h('td', {}, k === 'fast' ? `Fast burn (${b.long_window} / ${b.short_window})` : `Slow burn (${b.long_window} / ${b.short_window})`),
                h('td', {}, burnText(b)), h('td', {}, `${b.threshold}x`), h('td', {}, badge(b.firing ? 'firing' : 'ok', b.firing ? 'bad' : 'ok')));
        }), '')));
    replaceChildren('slo-content', cards);
}

// ---------- incidents ----------
function duration(seconds) {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

async function fetchIncidents() {
    const { incidents } = await getJSON('/api/incidents?limit=30');
    if (!incidents.length) {
        replaceChildren('incidents-content', panel('No incidents',
            h('p', { class: 'hint' }, 'Related alerts that open close together are grouped into one incident here.')));
        return;
    }
    replaceChildren('incidents-content', incidents.map(i => {
        const c = i.likely_cause;
        const start = i.first_ts;
        return panel(`#${i.id} ${i.title}`,
            h('p', {}, badge(i.status, i.status === 'open' ? 'bad' : 'ok'), ' ', badge(i.severity, SEV[i.severity] || 'info'),
                ` started ${ago(i.first_ts)}, ${i.status === 'open' ? 'open for' : 'lasted'} ${duration(i.duration_seconds)}`),
            c ? h('p', { class: 'summary' }, h('strong', {}, `Likely cause (${c.confidence} confidence): ${c.label}. `), c.reason) : null,
            simpleTable(['+ seconds', 'Event', 'Severity', 'Detail'], i.timeline.map(e => h('tr', {},
                h('td', {}, `+${e.ts - start}s`), h('td', {}, e.type === 'alert_opened' ? 'opened' : 'resolved'),
                h('td', {}, badge(e.severity, SEV[e.severity] || 'info')), h('td', {}, e.message))), ''));
    }));
}

// ---------- insights ----------
async function fetchInsights() {
    const d = await getJSON('/api/insights');
    setText('summary-text', d.summary);
    const src = document.getElementById('summary-source');
    src.textContent = d.summary_source === 'llm' ? 'AI-generated, advisory' : 'Rule-based';
    src.className = `badge ${d.summary_source === 'llm' ? 'info' : 'ok'}`;
    setText('summary-note', d.summary_source === 'llm'
        ? 'Written by an LLM from anonymised counts and numbers only (no names, hosts or alert text). Verify before acting.'
        : d.llm_enabled ? 'LLM summary is enabled but unavailable right now; showing the rule-based summary.'
            : 'AI summary is off (opt-in). This text is generated by deterministic rules.');
    replaceChildren('anomaly-list', d.anomalies.length ? d.anomalies.map(a => h('li', {}, badge(a.direction, 'warn'), ' ', a.message))
        : [h('li', {}, 'Nothing unusual compared with the recent baseline.')]);
    replaceChildren('forecast-list', d.forecasts.length
        ? d.forecasts.map(f => h('li', {}, `${f.metric}: projected to reach 100% in about ${f.days_to_full} day(s) (+${f.slope_per_day} points/day, fit r² ${f.r2})`))
        : [h('li', {}, 'No upward capacity trend detected (needs a few hours of history).')]);
}


// ---------- settings ----------
const $ = id => document.getElementById(id);
let editingService = null;

async function sendJSON(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { /* no JSON body */ }
    return { ok: r.ok, status: r.status, data };
}

function errorText(res) {
    const d = res.data && res.data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map(x => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ');
    return `request failed (HTTP ${res.status})`;
}

function showMsg(id, kind, text, items) {
    const nodes = [h('span', { class: kind }, text)];
    if (items && items.length) nodes.push(h('ul', {}, items.map(i => h('li', {}, i))));
    replaceChildren(id, nodes);
}

function numOrUndef(id) {
    const v = parseFloat($(id).value);
    return Number.isNaN(v) ? undefined : v;
}

function servicePayload() {
    const codes = $('f-status').value.split(',').map(x => parseInt(x.trim(), 10)).filter(n => !Number.isNaN(n));
    const p = {
        name: $('f-name').value.trim(), url: $('f-url').value.trim(), method: $('f-method').value,
        interval_seconds: numOrUndef('f-interval'), latency_warn_ms: numOrUndef('f-slow'),
        failures_before_alert: numOrUndef('f-fails'), expected_status: codes.length ? codes : [200],
    };
    const goal = numOrUndef('f-slo');
    if (goal !== undefined) p.slo = { target: goal, window_days: numOrUndef('f-slodays') || 7 };
    if ($('f-usekey').checked) {
        p.api_key = {
            value: $('f-key').value || null, header: $('f-header').value.trim() || 'Authorization',
            scheme: $('f-scheme').value.trim(), expires_on: $('f-expires').value || null,
        };
    }
    return p;
}

function resetServiceForm() {
    editingService = null;
    $('svc-form').reset();
    $('key-fields').hidden = true;
    $('key-hint').hidden = true;
    $('f-name').disabled = false;
    $('svc-cancel').hidden = true;
    $('svc-form-title').textContent = 'Add an API or service';
}

function editService(s) {
    editingService = s.name;
    $('f-name').value = s.name; $('f-name').disabled = true;       // the name identifies the service and its history
    $('f-url').value = '';                                          // full URLs are never sent back (they can hold tokens); blank keeps it
    $('f-url').placeholder = `blank keeps the current URL (${s.host})`;
    $('f-method').value = s.method; $('f-interval').value = s.interval_seconds; $('f-slow').value = s.latency_warn_ms;
    $('f-fails').value = s.failures_before_alert; $('f-status').value = s.expected_status.join(', ');
    $('f-slo').value = s.slo ? s.slo.target : ''; $('f-slodays').value = s.slo ? s.slo.window_days : 7;
    $('f-usekey').checked = s.key_configured;
    $('key-fields').hidden = !s.key_configured;
    $('f-key').value = '';
    $('f-header').value = s.key_header || 'Authorization'; $('f-scheme').value = s.key_scheme || '';
    $('f-expires').value = s.key_expires_on || '';
    $('key-hint').hidden = !s.key_configured;
    $('key-hint').textContent = s.key_set ? 'A key is saved. Leave the key box blank to keep it, or paste a new one to replace it.'
        : 'No key is saved yet. Paste it above.';
    $('svc-cancel').hidden = false;
    $('svc-form-title').textContent = `Edit ${s.name}`;
    $('svc-msg').textContent = '';
    $('svc-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function describeCheck(r, usedKey) {
    if (r.ok) {
        const extra = [];
        if (r.rl_remaining !== null && r.rl_remaining !== undefined)
            extra.push(`Quota: ${r.rl_remaining}${r.rl_limit ? ' of ' + r.rl_limit : ''} requests remaining (the provider sends rate-limit headers).`);
        else extra.push('This API sends no rate-limit headers, so no quota will be shown.');
        if (r.key_days_left !== null && r.key_days_left !== undefined) extra.push(`Key expires in ${r.key_days_left} day(s).`);
        if (r.note) extra.push(r.note);
        return { kind: 'good', text: `Working: HTTP ${r.status_code} in ${r.latency_ms} ms.`, items: extra };
    }
    const items = [];
    if (r.status_code === 401 || r.status_code === 403)
        items.push(usedKey ? 'The key was sent but the API rejected it. Check the key, the header name and the scheme (Bearer, Token, or blank).'
            : 'The API wants credentials. Tick "This API needs a key" and enter it.');
    if (r.status_code && !(r.status_code === 401 || r.status_code === 403))
        items.push('If this status is normal for the endpoint, add it to "Healthy status codes".');
    if (r.note) items.push(r.note);
    return { kind: 'problem', text: `Not healthy: ${r.error || 'failed'}${r.latency_ms ? ` (${r.latency_ms} ms)` : ''}.`, items };
}

async function fetchSettings() {
    const res = await sendJSON('GET', '/api/settings');
    $('settings-locked').hidden = res.ok;
    $('settings-body').hidden = !res.ok;
    if (!res.ok) { setText('settings-locked-msg', errorText(res)); return; }
    const { services, aws } = res.data;

    const rows = services.map(s => {
        let actions = '';
        if (s.editable) {
            const edit = h('button', { class: 'btn small', type: 'button' }, 'Edit');
            const remove = h('button', { class: 'btn small danger', type: 'button' }, 'Remove');
            edit.addEventListener('click', () => editService(s));
            remove.addEventListener('click', async () => {
                if (!confirm(`Stop monitoring ${s.name} and delete its saved key?`)) return;
                const r = await sendJSON('DELETE', `/api/settings/services/${encodeURIComponent(s.name)}`);
                if (!r.ok) showMsg('svc-msg', 'problem', errorText(r));
                else { if (editingService === s.name) resetServiceForm(); showMsg('svc-msg', 'good', `${s.name} removed.`); }
                fetchSettings();
            });
            actions = [edit, remove];
        }
        return h('tr', {},
            h('td', {}, h('strong', {}, s.name), h('div', { class: 'sub' }, s.host || '')),
            h('td', {}, s.source === 'ui' ? badge('dashboard', 'info') : badge('sentinel.yaml', 'ok')),
            h('td', {}, keyCell(null, s)),
            h('td', {}, `${s.interval_seconds}s`),
            h('td', {}, actions));
    });
    replaceChildren('settings-services-body', rows.length ? rows : [h('tr', {}, h('td', { colspan: 5 }, 'No services yet. Add one below.'))]);

    $('a-enabled').checked = aws.enabled || aws.source !== 'ui';
    $('a-regions').value = aws.regions.join(', ');
    $('a-budget').value = aws.monthly_budget_usd; $('a-warn').value = aws.budget_warn_pct;
    $('a-idlecpu').value = aws.idle_cpu_percent; $('a-idledays').value = aws.idle_days;
    $('a-interval').value = aws.interval_minutes; $('a-profile').value = aws.profile || '';
    $('a-keyid').value = ''; $('a-secret').value = ''; $('a-token').value = '';
    $('a-keyid').placeholder = aws.key_id_hint ? `saved key ${aws.key_id_hint} (leave blank to keep)` : 'AKIA...';
    $('a-secret').placeholder = aws.key_id_hint ? 'saved (leave blank to keep)' : '';
    setText('aws-status', aws.enabled
        ? `AWS monitoring is ON for ${aws.regions.join(', ')}. Credentials: ${aws.credentials}${aws.key_id_hint ? ' (' + aws.key_id_hint + ')' : ''}. Settings from: ${aws.source === 'ui' ? 'this dashboard' : 'sentinel.yaml'}.`
        : 'AWS monitoring is OFF. Nothing is collected and no AWS calls are made. Fill this in and save to turn it on.');
}

function awsPayload() {
    return {
        enabled: $('a-enabled').checked,
        regions: $('a-regions').value.split(',').map(x => x.trim()).filter(Boolean),
        profile: $('a-profile').value.trim() || null,
        access_key_id: $('a-keyid').value.trim() || null,
        secret_access_key: $('a-secret').value.trim() || null,
        session_token: $('a-token').value.trim() || null,
        use_environment: $('a-useenv').checked,
        monthly_budget_usd: numOrUndef('a-budget'), budget_warn_pct: numOrUndef('a-warn'),
        idle_cpu_percent: numOrUndef('a-idlecpu'), idle_days: numOrUndef('a-idledays'),
        interval_minutes: numOrUndef('a-interval'),
    };
}

function withBusy(btn, fn) {
    return async (...a) => {
        btn.disabled = true;
        try { await fn(...a); } catch (e) { console.error(e); } finally { btn.disabled = false; }
    };
}

function initSettings() {
    $('f-usekey').addEventListener('change', () => { $('key-fields').hidden = !$('f-usekey').checked; });
    $('svc-cancel').addEventListener('click', resetServiceForm);

    $('svc-test').addEventListener('click', withBusy($('svc-test'), async () => {
        showMsg('svc-msg', '', 'Checking...');
        const p = servicePayload();
        const res = await sendJSON('POST', '/api/settings/test/service', p);
        if (!res.ok) return showMsg('svc-msg', 'problem', errorText(res));
        const d = describeCheck(res.data, !!p.api_key);
        showMsg('svc-msg', d.kind, d.text, d.items);
    }));

    $('svc-form').addEventListener('submit', async ev => {
        ev.preventDefault();
        const btn = $('svc-save'); btn.disabled = true;
        try {
            const res = await sendJSON('PUT', '/api/settings/services', servicePayload());
            if (!res.ok) return showMsg('svc-msg', 'problem', errorText(res));
            const name = res.data.name;
            resetServiceForm();
            showMsg('svc-msg', 'good', `${name} saved. The first check is running now; see it in the Services tab.`);
            fetchSettings();
        } finally { btn.disabled = false; }
    });

    $('aws-test').addEventListener('click', withBusy($('aws-test'), async () => {
        showMsg('aws-msg', '', 'Testing the connection (read-only calls)...');
        const res = await sendJSON('POST', '/api/settings/test/aws', awsPayload());
        if (!res.ok) return showMsg('aws-msg', 'problem', errorText(res));
        const d = res.data;
        const items = d.checks.map(c => `${c.ok ? '✓' : '✗'} ${c.name}${c.error ? ' - ' + c.error : ''}`);
        if (d.warning) items.unshift(d.warning);
        if (d.ok) items.push('Cost Explorer is not tested here (each call is billed about $0.01); it is checked on the first collection after you save.');
        showMsg('aws-msg', d.ok ? 'good' : 'problem',
            d.ok ? `Connected to AWS account ${d.account_hint}. All read-only checks passed.`
                : (d.account_hint ? 'Connected, but some permissions are missing.' : 'Could not sign in to AWS.'), items);
    }));

    $('aws-form').addEventListener('submit', async ev => {
        ev.preventDefault();
        const res = await sendJSON('PUT', '/api/settings/aws', awsPayload());
        if (!res.ok) return showMsg('aws-msg', 'problem', errorText(res));
        $('a-useenv').checked = false;
        showMsg('aws-msg', res.data.enabled ? 'good' : '', res.data.enabled
            ? 'Saved. Collecting from AWS now; see the Cloud tab in a few seconds.' : 'Saved. AWS monitoring is off.');
        fetchSettings();
    });

    $('aws-reset').addEventListener('click', async () => {
        if (!confirm('Delete the AWS settings and keys saved from this dashboard? (sentinel.yaml is not touched.)')) return;
        const res = await sendJSON('DELETE', '/api/settings/aws');
        showMsg('aws-msg', res.ok ? 'good' : 'problem', res.ok ? 'Saved AWS settings and keys removed.' : errorText(res));
        fetchSettings();
    });
}

// ---------- polling ----------
const safe = fn => () => fn().catch(e => console.error(e));
function refreshActive() {
    const map = { overview: [fetchCurrent, fetchProcesses, fetchHistory], services: [fetchServices], cloud: [fetchCloud], slo: [fetchSlo], incidents: [fetchIncidents],
        insights: [fetchInsights], alerts: [fetchAlertHistory], settings: [fetchSettings] };
    (map[activeTab] || []).forEach(f => safe(f)());
}

function every(ms, fn) {
    setInterval(() => { if (!document.hidden) safe(fn)(); }, ms);
}

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initSettings();
    initTimeControls();
    refreshActive();
    safe(fetchOpenAlerts)();
    every(3000, fetchOpenAlerts);
    every(3000, () => activeTab === 'overview' ? fetchCurrent() : Promise.resolve());
    every(5000, () => activeTab === 'overview' ? fetchProcesses() : Promise.resolve());
    every(10000, () => activeTab === 'overview' ? fetchHistory() : Promise.resolve());
    every(10000, () => activeTab === 'services' ? fetchServices() : Promise.resolve());
    every(10000, () => activeTab === 'slo' ? fetchSlo() : Promise.resolve());
    every(5000, () => activeTab === 'incidents' ? fetchIncidents() : Promise.resolve());
    every(60000, () => activeTab === 'cloud' ? fetchCloud() : Promise.resolve());
    every(30000, () => activeTab === 'insights' ? fetchInsights() : Promise.resolve());
    every(10000, () => activeTab === 'alerts' ? fetchAlertHistory() : Promise.resolve());
});
