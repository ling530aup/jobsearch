async function request(url, options = {}) { const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options }); const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data; }
function queryParams(filters = {}, extra = {}) { const params = new URLSearchParams(); Object.entries({ ...filters, ...extra }).forEach(([key, value]) => { if (value === '' || value === null || value === undefined) return; if (Array.isArray(value)) value.forEach(item => params.append(key, item)); else params.set(key, value); }); return params; }
export const getBootstrap = () => request('/api/bootstrap');
export const getRuns = () => request('/api/runs');
export function getFacets(filters, exclude) { return request(`/api/facets?${queryParams(filters, { exclude })}`); }
export const getRun = id => request(`/api/runs/${id}`);
export const startRun = profile => request('/api/runs', { method: 'POST', body: JSON.stringify({ profile }) });
export const updateApplied = (id, applied) => request(`/api/jobs/${id}/applied`, { method: 'PATCH', body: JSON.stringify({ applied }) });
export function getJobs(filters) { return request(`/api/jobs?${queryParams(filters)}`); }
