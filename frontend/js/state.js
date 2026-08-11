export const state = { scope: 'latest', runId: null, filters: { company: [], location: [], date: [], title: '', applied: 'all' }, facets: { companies: [], locations: [], dates: [] }, jobs: [], nextCursor: null, total: 0, runs: [], activeSearchId: null, pollTimer: null };
export function queryForJobs(cursor = '') { return { scope: state.scope, run_id: state.runId, ...state.filters, limit: 50, cursor }; }
export function resetJobs() { state.jobs = []; state.nextCursor = null; state.total = 0; }
