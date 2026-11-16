'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let token = '', epoch = 0, controller = null, pending = false;
  const setStatus = (text, error = false) => {
    $('connection').textContent = text;
    $('connection').classList.toggle('error', error);
  };
  function controls() {
    $('refresh').disabled = !token || pending;
    $('download').disabled = !token || pending;
    $('disconnect').disabled = !token;
  }
  function clearView() {
    for (const id of ['requests', 'success', 'budget', 'latency']) $(id).textContent = '—';
    $('healing').textContent = $('deployment').textContent = '等待连接';
    $('business').replaceChildren(); $('events').replaceChildren();
    $('event-count').textContent = '0 EVENTS';
    $('updated').textContent = '手动刷新，不后台轮询';
  }
  function disconnect(message = '已断开，令牌与显示数据已清除') {
    epoch++; controller?.abort(); controller = null; token = ''; pending = false;
    $('token').value = ''; clearView(); controls(); setStatus(message);
  }
  async function get(path, signal) {
    const response = await fetch(path, {headers: {Authorization: `Bearer ${token}`}, signal, cache: 'no-store'});
    if (!response.ok) throw new Error(response.status === 401 ? '令牌无效或已失效，请重新连接。' : `读取失败（HTTP ${response.status}），可重试。`);
    return response;
  }
  const percentage = n => n == null ? '无数据' : `${(n * 100).toFixed(2)}%`;
  function render(data) {
    const slo = data.report.slo;
    $('requests').textContent = String(slo.total_requests);
    $('success').textContent = percentage(slo.success_ratio);
    $('budget').textContent = percentage(slo.error_budget_consumed_ratio);
    $('latency').textContent = slo.p95_seconds == null ? '无数据' : `${(slo.p95_seconds * 1000).toFixed(1)} ms`;
    const rows = [['代码版本', data.application.version], ['故障', data.application.fault?.kind ?? '无活动故障'], ['剩余时间', `${data.application.remaining_seconds.toFixed(1)} 秒`], ['缓存降级', data.application.fallback_enabled ? '已启用' : '关闭'], ['真实自动动作', '未启用']];
    const nodes = [];
    for (const [key, value] of rows) {
      const dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = key; dd.textContent = value; nodes.push(dt, dd);
    }
    $('business').replaceChildren(...nodes);
    const h = data.healing;
    $('healing').textContent = JSON.stringify({mode: h.mode, enabled: h.enabled, services: h.services}, null, 2);
    const d = data.deployment;
    $('deployment').textContent = JSON.stringify({phase: d.phase, active: d.active, previous: d.previous, candidate: d.candidate, outcome: d.last_outcome, locked: d.locked}, null, 2);
    $('event-count').textContent = `${data.events.length} EVENTS`;
    const eventNodes = data.events.map(event => {
      const item = document.createElement('div'); item.className = 'event';
      const time = document.createElement('time'); time.textContent = new Date(event.timestamp).toLocaleString();
      const content = document.createElement('div'), title = document.createElement('strong'), detail = document.createElement('p');
      title.textContent = event.action; detail.textContent = JSON.stringify(event.details);
      content.append(title, detail); item.append(time, content); return item;
    });
    if (!eventNodes.length) {const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '暂无操作事件。'; eventNodes.push(empty);}
    $('events').replaceChildren(...eventNodes);
    $('updated').textContent = `最后更新 ${new Date().toLocaleTimeString()} · 手动刷新`;
  }
  async function refresh() {
    if (!token || pending) return;
    const current = epoch; controller = new AbortController(); pending = true; controls(); setStatus('正在读取当前快照…');
    try {
      const response = await get('/lab/overview', controller.signal);
      const data = await response.json();
      if (current !== epoch) return;
      render(data); setStatus('已连接 · 只读快照，不自动执行任何动作');
    } catch (error) {
      if (current !== epoch || error.name === 'AbortError') return;
      clearView(); setStatus(error.message, true);
    } finally { if (current === epoch) {pending = false; controls();} }
  }
  $('access').addEventListener('submit', event => {
    event.preventDefault(); const value = $('token').value.trim();
    if (!value) return;
    epoch++; controller?.abort(); token = value; $('token').value = ''; pending = false;
    clearView(); controls(); refresh();
  });
  $('disconnect').addEventListener('click', () => disconnect());
  $('refresh').addEventListener('click', refresh);
  $('download').addEventListener('click', async () => {
    if (!token || pending) return;
    const current = epoch; pending = true; controller = new AbortController(); controls();
    try {
      const response = await get('/lab/report?format=markdown', controller.signal);
      const blob = await response.blob(); if (current !== epoch) return;
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = 'sre-lab-report.md'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); setStatus('复盘报告已下载，只包含当前进程观测');
    } catch (error) {if (current === epoch && error.name !== 'AbortError') setStatus(error.message, true);}
    finally {if (current === epoch) {pending = false; controls();}}
  });
  window.addEventListener('pagehide', () => disconnect('页面已离开，请重新连接'));
})();
