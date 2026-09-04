import { copyFile, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline/promises';
import { isDeepStrictEqual } from 'node:util';
import { chromium } from 'playwright';

const SHOWCASE_EMAIL = 'sibyl-showcase@localhost';
const SHOWCASE_NAME = 'Sibyl Showcase';
const SHOWCASE_SLUG = 'sibyl-showcase';
const WEB_URL = process.env.SIBYL_SHOWCASE_WEB_URL ?? 'http://localhost:3337';
const ROOT = fileURLToPath(new URL('../../..', import.meta.url));
const MANIFEST_PATH = join(ROOT, '.moon/cache/showcase-runtime-manifest.json');
const FILTER_CONFIG_PATH = join(ROOT, '.moon/cache/showcase-private-terms.json');

const CAPTURES = [
  {
    route: '/',
    output: 'docs/public/screenshots/web-dashboard.png',
    copy: 'docs/images/dashboard.png',
    readyText: 'Recent Activity',
  },
  {
    route: '/projects',
    output: 'docs/public/screenshots/web-projects.png',
    copy: 'docs/images/projects.png',
    readyText: 'Hypercolor',
  },
  {
    route: '/graph',
    output: 'docs/public/screenshots/web-graph.png',
    copy: 'docs/images/graph.png',
    canvas: true,
  },
  {
    route: '/tasks',
    output: 'docs/public/screenshots/web-tasks.png',
    copy: 'docs/images/tasks.png',
    readyText: 'Add relationship filters to graph search',
  },
  {
    route: '/entities',
    output: 'docs/public/screenshots/web-entities.png',
    readyText: 'Project scope belongs on every graph write',
  },
  {
    route: '/search',
    output: 'docs/public/screenshots/web-search.png',
    search: true,
  },
  {
    route: '/sources',
    output: 'docs/public/screenshots/web-sources.png',
    readyText: 'Sibyl on GitHub',
  },
];

export function requireLoopback(rawUrl) {
  const url = new URL(rawUrl);
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    !['localhost', '127.0.0.1', '::1'].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new Error('Showcase capture only accepts a loopback web server.');
  }
  return url.origin;
}

export function parseForbiddenTerms(config) {
  const rawTerms = config?.forbidden_terms;
  if (!Array.isArray(rawTerms) || rawTerms.length === 0) {
    throw new Error('Screenshot filter config needs a non-empty forbidden_terms list.');
  }
  const terms = rawTerms.map(term =>
    typeof term === 'string' ? term.trim().toLocaleLowerCase() : ''
  );
  if (terms.some(term => !term) || new Set(terms).size !== terms.length) {
    throw new Error('Screenshot filter terms must be unique, non-empty strings.');
  }
  return terms;
}

export function assertNoForbiddenTerms(value, surface, forbiddenTerms) {
  const text = JSON.stringify(value).toLocaleLowerCase();
  if (forbiddenTerms.some(term => text.includes(term))) {
    throw new Error(`${surface} contains forbidden private content.`);
  }
}

export function validateSession(me, orgs, manifest, forbiddenTerms) {
  assertNoForbiddenTerms({ me, orgs }, 'Capture account', forbiddenTerms);
  if (me.user?.email !== SHOWCASE_EMAIL || me.user?.name !== SHOWCASE_NAME) {
    throw new Error(`Sign in as the dedicated ${SHOWCASE_EMAIL} account.`);
  }
  if (
    me.organization?.slug !== SHOWCASE_SLUG ||
    me.organization?.id !== manifest.organization.id ||
    me.organization?.name !== SHOWCASE_NAME
  ) {
    throw new Error('The browser is not scoped to the verified showcase organization.');
  }

  const unexpected = orgs.orgs.filter(org => {
    if (org.slug === SHOWCASE_SLUG) {
      return org.name !== SHOWCASE_NAME || org.is_personal;
    }
    return !org.is_personal || org.name !== SHOWCASE_NAME || !/^u-[0-9a-f-]+$/.test(org.slug);
  });
  if (unexpected.length > 0) {
    throw new Error('The capture account belongs to an unexpected organization.');
  }
}

function entityProjection(entity) {
  return {
    id: String(entity.id ?? ''),
    entity_type: String(entity.entity_type ?? ''),
    name: String(entity.name ?? ''),
    description: String(entity.description ?? ''),
    content: String(entity.content ?? ''),
    category: entity.category ?? null,
    languages: [...(entity.languages ?? [])],
    tags: [...(entity.tags ?? [])],
    metadata: { ...(entity.metadata ?? {}) },
    source_file: entity.source_file ?? null,
    created_at: entity.created_at ?? null,
    updated_at: entity.updated_at ?? null,
    related: entity.related ?? null,
    background_jobs: { ...(entity.background_jobs ?? {}) },
    probe_rehearsal: entity.probe_rehearsal ?? null,
  };
}

function sourceProjection(source) {
  return {
    id: String(source.id ?? ''),
    name: String(source.name ?? ''),
    url: String(source.url ?? ''),
    source_type: String(source.source_type ?? ''),
    description: String(source.description ?? ''),
    crawl_depth: source.crawl_depth ?? null,
    include_patterns: [...(source.include_patterns ?? [])],
    exclude_patterns: [...(source.exclude_patterns ?? [])],
    crawl_status: String(source.crawl_status ?? ''),
    document_count: source.document_count ?? null,
    chunk_count: source.chunk_count ?? null,
    last_crawled_at: source.last_crawled_at ?? null,
    last_error: source.last_error ?? null,
    created_at: source.created_at ?? null,
  };
}

export function buildCorpusSnapshot(entities, sources) {
  const compareKeys = (left, right) => (left < right ? -1 : left > right ? 1 : 0);
  return {
    entities: entities
      .map(entityProjection)
      .sort((left, right) =>
        compareKeys(
          [left.entity_type, left.name, left.id].join('\0'),
          [right.entity_type, right.name, right.id].join('\0')
        )
      ),
    sources: sources
      .map(sourceProjection)
      .sort((left, right) =>
        compareKeys(
          [left.name, left.url, left.id].join('\0'),
          [right.name, right.url, right.id].join('\0')
        )
      ),
  };
}

export function validateCorpusSnapshot(actual, manifest, forbiddenTerms) {
  assertNoForbiddenTerms(actual, 'Authenticated showcase corpus', forbiddenTerms);
  if (!manifest.corpus || !isDeepStrictEqual(actual, manifest.corpus)) {
    throw new Error('The authenticated showcase corpus changed after the seed verification.');
  }
}

export function validateGraphPayload(payload, forbiddenTerms) {
  if (!payload || !Array.isArray(payload.nodes)) {
    throw new Error('Showcase capture received an invalid graph payload.');
  }
  assertNoForbiddenTerms(payload, 'Graph payload', forbiddenTerms);
}

export function assertCompleteSourcePage(sourcePage) {
  const sources = sourcePage.sources ?? [];
  if (!Number.isInteger(sourcePage.total) || sourcePage.total !== sources.length) {
    throw new Error('Showcase capture refuses truncated source results.');
  }
  return sources;
}

async function fetchJson(page, path, init) {
  return page.evaluate(
    async ({ requestPath, requestInit }) => {
      const response = await fetch(requestPath, requestInit);
      if (!response.ok) {
        throw new Error(`${requestPath} returned HTTP ${response.status}`);
      }
      return response.json();
    },
    { requestPath: path, requestInit: init }
  );
}

function isGraphPayloadResponse(response) {
  const url = new URL(response.url());
  return (
    response.request().method() === 'GET' && url.pathname === '/api/graph/hierarchical'
  );
}

export async function readGraphPayload(response, forbiddenTerms) {
  if (!response.ok()) {
    throw new Error('Showcase capture could not verify the graph payload.');
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error('Showcase capture could not parse the graph payload.');
  }
  validateGraphPayload(payload, forbiddenTerms);
}

async function bindShowcaseSession(page, manifest, forbiddenTerms) {
  let me = await fetchJson(page, '/api/auth/me');
  if (me.organization?.slug !== SHOWCASE_SLUG) {
    await fetchJson(page, `/api/orgs/${SHOWCASE_SLUG}/switch`, { method: 'POST' });
    await page.reload({ waitUntil: 'domcontentloaded' });
    me = await fetchJson(page, '/api/auth/me');
  }
  const orgs = await fetchJson(page, '/api/orgs');
  validateSession(me, orgs, manifest, forbiddenTerms);

  await page.getByRole('button', { name: 'Open user menu' }).click();
  await page.getByRole('menu').waitFor({ state: 'visible' });
  assertNoForbiddenTerms(
    await page.getByRole('menu').innerText(),
    'Account menu',
    forbiddenTerms
  );
  await page.keyboard.press('Escape');
}

async function verifyShowcaseCorpus(page, manifest, forbiddenTerms) {
  const me = await fetchJson(page, '/api/auth/me');
  if (me.organization?.id !== manifest.organization.id) {
    throw new Error('The browser left the verified showcase organization.');
  }

  const entityPage = await fetchJson(
    page,
    '/api/entities?page_size=200&sort_by=name&sort_order=asc'
  );
  if (entityPage.has_more) {
    throw new Error('Showcase capture refuses organizations over 200 entities.');
  }
  const entities = await Promise.all(
    (entityPage.entities ?? []).map(entity =>
      fetchJson(
        page,
        `/api/entities/${encodeURIComponent(entity.id)}?include_summary=false&related_limit=0`
      )
    )
  );
  const sourcePage = await fetchJson(page, '/api/sources?limit=200');
  const sources = assertCompleteSourcePage(sourcePage);
  validateCorpusSnapshot(buildCorpusSnapshot(entities, sources), manifest, forbiddenTerms);
}

async function waitForCaptureSurface(page, capture, forbiddenTerms) {
  await page.getByRole('main').waitFor({ state: 'visible', timeout: 20_000 });
  if (capture.canvas) {
    await page.locator('canvas').waitFor({ state: 'visible', timeout: 20_000 });
    await page.waitForTimeout(3_000);
  } else if (capture.search) {
    const form = page.getByRole('main').locator('form').first();
    await form.getByLabel('Search', { exact: true }).fill(
      'Project scope belongs on every graph write'
    );
    await form.getByRole('button', { name: 'Search', exact: true }).click();
    await page.getByRole('heading', {
      name: 'Project scope belongs on every graph write',
      exact: true,
    }).first().waitFor({ state: 'visible', timeout: 20_000 });
  } else if (capture.readyText) {
    await page.getByText(capture.readyText, { exact: true }).first().waitFor({
      state: 'visible',
      timeout: 20_000,
    });
  }
  assertNoForbiddenTerms(
    await page.locator('body').innerText(),
    capture.route,
    forbiddenTerms
  );
}

async function main() {
  const webUrl = requireLoopback(WEB_URL);
  const forbiddenTerms = parseForbiddenTerms(
    JSON.parse(await readFile(FILTER_CONFIG_PATH, 'utf8'))
  );
  const manifest = JSON.parse(await readFile(MANIFEST_PATH, 'utf8'));
  assertNoForbiddenTerms(manifest, 'Showcase manifest', forbiddenTerms);
  if (
    manifest.organization?.slug !== SHOWCASE_SLUG ||
    manifest.organization?.name !== SHOWCASE_NAME
  ) {
    throw new Error('Run moon run showcase-seed before capturing screenshots.');
  }
  const createdRows = Object.values(manifest.created ?? {}).reduce(
    (total, count) => total + Number(count),
    0
  );
  if (createdRows !== 0) {
    throw new Error('The pre-capture seed created rows. Rerun capture after inspecting the corpus.');
  }

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({
    viewport: { width: 1322, height: 916 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  const terminal = createInterface({ input: process.stdin, output: process.stdout });
  const captureDirectory = await mkdtemp(join(tmpdir(), 'sibyl-showcase-'));

  try {
    await page.goto(`${webUrl}/login`, { waitUntil: 'domcontentloaded' });
    await terminal.question(
      `Sign in as ${SHOWCASE_EMAIL} in the browser, then press Enter here to verify and capture. `
    );
    await page.waitForURL(url => url.pathname !== '/login', { timeout: 20_000 });
    await bindShowcaseSession(page, manifest, forbiddenTerms);

    for (const [index, capture] of CAPTURES.entries()) {
      const graphPayload = capture.canvas
        ? page.waitForResponse(isGraphPayloadResponse, { timeout: 20_000 })
        : null;
      await page.goto(`${webUrl}${capture.route}`, { waitUntil: 'domcontentloaded' });
      if (graphPayload) {
        await readGraphPayload(await graphPayload, forbiddenTerms);
      }
      await waitForCaptureSurface(page, capture, forbiddenTerms);
      await verifyShowcaseCorpus(page, manifest, forbiddenTerms);
      assertNoForbiddenTerms(
        await page.locator('body').innerText(),
        capture.route,
        forbiddenTerms
      );
      await page.screenshot({ path: join(captureDirectory, `${index}.png`) });
      console.log(`staged ${capture.route} for ${capture.output}`);
    }

    for (const [index, capture] of CAPTURES.entries()) {
      const staged = join(captureDirectory, `${index}.png`);
      await copyFile(staged, join(ROOT, capture.output));
      if (capture.copy) {
        await copyFile(staged, join(ROOT, capture.copy));
      }
      console.log(`captured ${capture.route} -> ${capture.output}`);
    }
  } finally {
    terminal.close();
    await browser.close();
    await rm(captureDirectory, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await main();
}
