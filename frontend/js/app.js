import { getBootstrap, getFacets, getJobs, getRun, getRuns, startRun, updateApplied } from './api.js';
import { queryForJobs, resetJobs, state } from './state.js';
import { renderActiveRun, renderJobs, renderLatestRun, renderRuns, setProfiles, setTab, toast } from './ui.js';

const $ = id => document.getElementById(id);
let openFacet = null;
let pendingFacetValues = [];
const FACET_TYPES = ['company', 'location', 'date'];
const FACET_LABELS = { company: '公司', location: '地点', date: '日期' };
async function loadJobs({ append = false } = {}) { $('loading-copy').textContent = '正在读取数据库…'; try { const data = await getJobs(queryForJobs(append ? state.nextCursor : '')); if (append) state.jobs.push(...data.jobs); else state.jobs = data.jobs; state.nextCursor = data.next_cursor; state.total = data.total; renderJobs(state); } catch (error) { toast(error.message, 'error'); } }
async function loadRuns() { try { const data = await getRuns(); state.runs = data.runs; renderRuns(data.runs); renderLatestRun(data.runs); } catch (error) { toast(error.message, 'error'); } }
async function loadFacets(exclude = 'company') { try { state.facets = await getFacets(queryForJobs(), exclude); updateFacetTriggers(); } catch (error) { toast(error.message, 'error'); } }
async function refreshResults() { resetJobs(); await loadJobs(); }
async function showRunResults(runId) { state.scope = 'all'; state.runId = Number(runId); document.querySelectorAll('.scope-button').forEach(button => button.classList.toggle('active', button.dataset.scope === 'all')); Object.assign(state.filters, { company: [], location: [], date: [], title: '', applied: 'all' }); $('table-title-filter').value = ''; document.querySelectorAll('[data-applied]').forEach(button => button.classList.toggle('active', button.dataset.applied === 'all')); updateFacetTriggers(); setTab('results'); await loadFacets(); await refreshResults(); }
function scheduleRunPoll(delay = 1200) { clearTimeout(state.pollTimer); state.pollTimer = setTimeout(pollRun, delay); }
async function pollRun() { if (!state.activeSearchId) return; try { const run = await getRun(state.activeSearchId); renderActiveRun(run); if (run.status === 'running') scheduleRunPoll(); else { state.activeSearchId = null; loadRuns(); refreshResults(); } } catch (error) { toast(error.message, 'error'); scheduleRunPoll(2000); } }
function facetLabel(type) { return FACET_LABELS[type]; }
function facetValues(type) { return state.facets[type === 'company' ? 'companies' : type === 'location' ? 'locations' : 'dates'] || []; }
function updateFacetTriggers() { FACET_TYPES.forEach(type => { const count = state.filters[type].length; $(`${type}-facet-trigger`).textContent = count ? `${facetLabel(type)} · ${count} 项` : `筛选${facetLabel(type)}`; }); }
function renderFacetOptions() { const term = $('facet-search').value.trim().toLowerCase(); const values = facetValues(openFacet); $('facet-options').innerHTML = values.filter(value => String(value).toLowerCase().includes(term)).map(value => `<label><input type="checkbox" value="${String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;')}" ${pendingFacetValues.includes(String(value)) ? 'checked' : ''} /><span>${value}</span></label>`).join('') || '<p class="facet-empty">没有匹配选项</p>'; }
async function openFacetMenu(type, trigger) { openFacet = type; pendingFacetValues = [...state.filters[type]]; $('facet-menu-title').textContent = `筛选${facetLabel(type)}`; $('facet-search').value = ''; const rect = trigger.getBoundingClientRect(); const menu = $('facet-menu'); menu.classList.remove('hidden'); menu.style.left = `${Math.min(rect.left, window.innerWidth - 330)}px`; menu.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 430)}px`; $('facet-options').innerHTML = '<p class="facet-empty">正在更新可选项…</p>'; try { state.facets = await getFacets(queryForJobs(), type); renderFacetOptions(); $('facet-search').focus(); } catch (error) { closeFacetMenu(); toast(error.message, 'error'); } }
function closeFacetMenu() { $('facet-menu').classList.add('hidden'); openFacet = null; }

document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));
document.querySelectorAll('.scope-button').forEach(button => button.addEventListener('click', async () => { state.scope = button.dataset.scope; state.runId = null; state.filters.company = []; state.filters.location = []; state.filters.date = []; document.querySelectorAll('.scope-button').forEach(item => item.classList.toggle('active', item === button)); await loadFacets(); await refreshResults(); }));
let filterTimer;
function bindFilter(inputId, key) { $(inputId).addEventListener('input', event => { state.filters[key] = event.target.value; clearTimeout(filterTimer); filterTimer = setTimeout(refreshResults, 220); }); }
bindFilter('table-title-filter', 'title');
document.querySelectorAll('[data-applied]').forEach(button => button.addEventListener('click', () => { state.filters.applied = button.dataset.applied; document.querySelectorAll('[data-applied]').forEach(item => item.classList.toggle('active', item === button)); refreshResults(); }));
$('clear-filters').addEventListener('click', () => { Object.keys(state.filters).forEach(key => state.filters[key] = key === 'applied' ? 'all' : Array.isArray(state.filters[key]) ? [] : ''); $('table-title-filter').value = ''; document.querySelectorAll('[data-applied]').forEach(button => button.classList.toggle('active', button.dataset.applied === 'all')); updateFacetTriggers(); refreshResults(); });
$('load-more').addEventListener('click', () => loadJobs({ append: true }));
$('jobs-body').addEventListener('change', async event => { if (!event.target.matches('.applied-toggle')) return; const checkbox = event.target; const id = Number(checkbox.dataset.jobId); const previous = !checkbox.checked; checkbox.disabled = true; const job = state.jobs.find(item => item.id === id); if (job) job.applied = checkbox.checked; try { await updateApplied(id, checkbox.checked); toast(checkbox.checked ? '已标记为已申请' : '已恢复为未申请'); } catch (error) { checkbox.checked = previous; if (job) job.applied = previous; toast(error.message, 'error'); } finally { checkbox.disabled = false; } });
$('start-run').addEventListener('click', async () => { const button = $('start-run'); button.disabled = true; try { const run = await startRun($('run-profile').value); state.activeSearchId = run.id; setTab('runs'); renderActiveRun(run); pollRun(); } catch (error) { toast(error.message, 'error'); } finally { button.disabled = false; } });
$('view-current-run').addEventListener('click', event => showRunResults(event.currentTarget.dataset.runId));
$('open-latest-run').addEventListener('click', event => showRunResults(event.currentTarget.dataset.runId));
$('runs-list').addEventListener('click', event => { const row = event.target.closest('[data-run-id]'); if (row) showRunResults(row.dataset.runId); });
$('refresh-runs').addEventListener('click', loadRuns);
$('company-facet-trigger').addEventListener('click', event => openFacetMenu('company', event.currentTarget));
$('location-facet-trigger').addEventListener('click', event => openFacetMenu('location', event.currentTarget));
$('date-facet-trigger').addEventListener('click', event => openFacetMenu('date', event.currentTarget));
$('facet-search').addEventListener('input', renderFacetOptions);
$('facet-options').addEventListener('change', event => { if (!event.target.matches('input[type=checkbox]')) return; pendingFacetValues = event.target.checked ? [...new Set([...pendingFacetValues, event.target.value])] : pendingFacetValues.filter(value => value !== event.target.value); });
$('facet-clear').addEventListener('click', () => { pendingFacetValues = []; renderFacetOptions(); });
$('facet-apply').addEventListener('click', () => { state.filters[openFacet] = pendingFacetValues; updateFacetTriggers(); closeFacetMenu(); refreshResults(); });
$('close-facet-menu').addEventListener('click', closeFacetMenu);
document.addEventListener('click', event => { const menu = $('facet-menu'); if (!menu.classList.contains('hidden') && !menu.contains(event.target) && !event.target.matches('[data-facet]')) closeFacetMenu(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeFacetMenu(); });

try { const data = await getBootstrap(); state.jobs = data.jobs; state.total = data.total; state.nextCursor = data.next_cursor; state.runs = data.runs; setProfiles(data.profiles); renderJobs(state); renderRuns(data.runs); renderLatestRun(data.runs); await loadFacets(); if (data.active_run) { state.activeSearchId = data.active_run.id; setTab('runs'); renderActiveRun(data.active_run); pollRun(); } } catch (error) { toast(error.message, 'error'); }
