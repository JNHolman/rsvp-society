import { state } from './state.js';

const DEFAULT_MAX_PAGE_REQUESTS = 250;

export async function apiFetch(path, options = {}, config = {}) {
  const { auth = true } = config;
  const { headers = {}, body, ...rest } = options;
  const finalHeaders = { ...headers };

  if (auth && state.adminToken) {
    finalHeaders['x-admin-token'] = state.adminToken;
  }

  let finalBody = body;
  if (body !== undefined && body !== null && typeof body !== 'string' && !(body instanceof FormData)) {
    finalBody = JSON.stringify(body);
    if (!finalHeaders['content-type']) {
      finalHeaders['content-type'] = 'application/json';
    }
  }

  return fetch(`${state.apiBase}${path}`, {
    ...rest,
    headers: finalHeaders,
    body: finalBody,
  });
}

export async function apiJson(path, options = {}, config = {}) {
  const response = await apiFetch(path, options, config);
  let data = {};

  try {
    data = await response.json();
  } catch {
    data = {};
  }

  if (!response.ok || data.ok === false) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }

  return data;
}

export function getNextPageSpec(payload, nextPageFallback) {
  const tokenPairs = [
    ['next_page_token', 'next_page_token'],
    ['nextPageToken', 'nextPageToken'],
    ['nextToken', 'nextToken'],
    ['token', 'token'],
    ['cursor', 'cursor'],
    ['nextCursor', 'nextCursor'],
    ['lastEvaluatedKey', 'lastEvaluatedKey'],
  ];

  for (const [field, param] of tokenPairs) {
    if (payload?.[field]) return { param, value: payload[field] };
  }

  const currentPage = Number(payload?.page ?? payload?.pageNumber ?? payload?.currentPage);
  const totalPages = Number(payload?.totalPages ?? payload?.pages);
  if (Number.isFinite(currentPage) && Number.isFinite(totalPages) && currentPage < totalPages) {
    return { param: 'page', value: currentPage + 1 };
  }

  if (payload?.hasMore === true || payload?.more === true) {
    return { param: 'page', value: nextPageFallback };
  }

  return null;
}

export function withQuery(path, param, value) {
  const url = new URL(path, 'https://placeholder.local');
  url.searchParams.set(param, typeof value === 'string' ? value : JSON.stringify(value));
  return `${url.pathname}${url.search}`;
}

function resolveMaxPages(options = {}) {
  if (Number.isFinite(options.maxPages) && options.maxPages > 0) return options.maxPages;

  const configured = Number.parseInt(localStorage.getItem('rsvp_max_page_requests') || '', 10);
  if (Number.isFinite(configured) && configured > 0) return configured;

  return DEFAULT_MAX_PAGE_REQUESTS;
}

export async function fetchAllPages(basePath, itemExtractor, options = {}) {
  const maxPages = resolveMaxPages(options);
  let path = basePath;
  let pageFallback = 2;
  const pages = [];
  const seen = new Set();

  const seedPages = Array.isArray(options.initialPages) ? options.initialPages.filter(Boolean) : [];
  if (seedPages.length) {
    pages.push(...seedPages);
    const firstSeedPath = options.initialPath || basePath;
    seen.add(firstSeedPath);

    const lastSeed = seedPages[seedPages.length - 1];
    const nextAfterSeed = getNextPageSpec(lastSeed, pageFallback);
    if (!nextAfterSeed) {
      const items = pages.flatMap((payload) => {
        const extracted = itemExtractor(payload);
        return Array.isArray(extracted) ? extracted : [];
      });
      return { pages, items };
    }

    path = withQuery(basePath, nextAfterSeed.param, nextAfterSeed.value);
    pageFallback += seedPages.length;
  } else {
    seen.add(path);
  }

  while (pages.length < maxPages) {
    const payload = await apiJson(path, options.requestOptions || {}, options.requestConfig || {});
    pages.push(payload);

    const nextSpec = getNextPageSpec(payload, pageFallback);
    if (!nextSpec) break;

    const nextPath = withQuery(basePath, nextSpec.param, nextSpec.value);
    if (seen.has(nextPath)) break;
    seen.add(nextPath);
    path = nextPath;
    pageFallback += 1;
  }

  const lastPayload = pages[pages.length - 1];
  if (pages.length >= maxPages && getNextPageSpec(lastPayload, pageFallback)) {
    const error = new Error(`Pagination limit reached for ${basePath}. Increase rsvp_max_page_requests or add a backend summary endpoint.`);
    error.code = 'PAGINATION_LIMIT_REACHED';
    error.path = basePath;
    error.pagesFetched = pages.length;
    throw error;
  }

  const items = pages.flatMap((payload) => {
    const extracted = itemExtractor(payload);
    return Array.isArray(extracted) ? extracted : [];
  });

  return { pages, items };
}
