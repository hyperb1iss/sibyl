import assert from 'node:assert/strict';
import test from 'node:test';
import {
  assertCompleteSourcePage,
  assertDarkTheme,
  assertNoForbiddenTerms,
  buildCorpusSnapshot,
  parseForbiddenTerms,
  readGraphPayload,
  requireLoopback,
  validateCorpusSnapshot,
  validateGraphPayload,
  validateSession,
} from './capture-showcase.mjs';

const forbiddenTerms = ['private customer'];

const manifest = { organization: { id: 'showcase-id', slug: 'sibyl-showcase' } };
const me = {
  user: { email: 'sibyl-showcase@localhost', name: 'Sibyl Showcase' },
  organization: { id: 'showcase-id', slug: 'sibyl-showcase', name: 'Sibyl Showcase' },
};
const orgs = {
  orgs: [
    { slug: 'sibyl-showcase', name: 'Sibyl Showcase', is_personal: false },
    {
      slug: 'u-3e2cfa93-35f5-4070-8327-9ce9f918b983',
      name: 'Sibyl Showcase',
      is_personal: true,
    },
  ],
};

test('capture accepts only loopback web servers', () => {
  assert.equal(requireLoopback('http://localhost:3337/path'), 'http://localhost:3337');
  assert.throws(() => requireLoopback('https://sibyl.example.com'), /loopback/);
  assert.throws(() => requireLoopback('http://localhost:3337?org=private'), /loopback/);
});

test('capture accepts the dedicated showcase identity and organizations', () => {
  assert.doesNotThrow(() => validateSession(me, orgs, manifest, forbiddenTerms));
});

test('capture requires the neon dark theme', () => {
  assert.doesNotThrow(() => assertDarkTheme('neon'));
  assert.throws(() => assertDarkTheme('dawn'), /requires the neon dark theme/);
});

test('capture refuses a normal account with another team organization', () => {
  const unsafeOrgs = {
    orgs: [...orgs.orgs, { slug: 'private', name: 'Private Work', is_personal: false }],
  };

  assert.throws(
    () => validateSession(me, unsafeOrgs, manifest, forbiddenTerms),
    /unexpected organization/
  );
});

test('capture refuses forbidden text in account chrome', () => {
  assert.throws(
    () =>
      assertNoForbiddenTerms(
        { organization: 'Private Customer' },
        'Account menu',
        forbiddenTerms
      ),
    /forbidden private content/
  );
});

test('capture refuses forbidden graph labels rendered on canvas', () => {
  assert.throws(
    () =>
      validateGraphPayload(
        {
          nodes: [{ id: 'safe-id', label: 'Private Customer roadmap' }],
          edges: [],
        },
        forbiddenTerms
      ),
    /forbidden private content/
  );
});

test('graph payload parse failures do not echo response content', async () => {
  const response = {
    ok: () => true,
    json: async () => {
      throw new Error(`Malformed JSON near ${forbiddenTerms[0]}`);
    },
  };

  await assert.rejects(() => readGraphPayload(response, forbiddenTerms), error => {
    assert.match(error.message, /could not parse the graph payload/);
    assert.doesNotMatch(error.message, new RegExp(forbiddenTerms[0], 'i'));
    return true;
  });
});

test('capture requires a valid local filter config', () => {
  assert.deepEqual(parseForbiddenTerms({ forbidden_terms: ['Private Customer'] }), [
    'private customer',
  ]);
  assert.throws(() => parseForbiddenTerms({ forbidden_terms: [] }), /non-empty/);
  assert.throws(
    () => parseForbiddenTerms({ forbidden_terms: ['duplicate', 'Duplicate'] }),
    /unique/
  );
});

test('capture refuses the right identity in the wrong organization', () => {
  const wrongOrg = {
    ...me,
    organization: { id: 'private-id', slug: 'private', name: 'Private Work' },
  };

  assert.throws(
    () => validateSession(wrongOrg, orgs, manifest, forbiddenTerms),
    /verified showcase/
  );
});

test('capture compares the authenticated corpus with the sealed seed snapshot', () => {
  const entities = [
    {
      id: 'project:sibyl',
      entity_type: 'project',
      name: 'Sibyl',
      description: 'Public project',
      content: 'Public project',
      category: 'showcase',
      languages: ['Python'],
      tags: ['sibyl-showcase', 'project'],
      metadata: { capture_mode: 'showcase', technologies: ['SurrealDB'] },
    },
  ];
  const sources = [];
  const corpus = buildCorpusSnapshot(entities, sources);
  const sealedManifest = { corpus };

  assert.doesNotThrow(() =>
    validateCorpusSnapshot(corpus, sealedManifest, forbiddenTerms)
  );
  const serverBookkeeping = buildCorpusSnapshot(
    [
      {
        ...entities[0],
        metadata: {
          ...entities[0].metadata,
          embedding_metadata: { provider: 'local' },
          retrieval_count: 12,
          revision: 3,
        },
      },
    ],
    sources
  );
  assert.deepEqual(serverBookkeeping, corpus);
  const drifted = buildCorpusSnapshot(
    [{ ...entities[0], metadata: { ...entities[0].metadata, assignees: ['private@example.com'] } }],
    sources
  );
  assert.throws(
    () => validateCorpusSnapshot(drifted, sealedManifest, forbiddenTerms),
    /changed after/
  );
});

test('capture refuses a truncated source response', () => {
  assert.deepEqual(assertCompleteSourcePage({ sources: [{ id: 'one' }], total: 1 }), [
    { id: 'one' },
  ]);
  assert.throws(
    () => assertCompleteSourcePage({ sources: [{ id: 'one' }], total: 2 }),
    /truncated source/
  );
});
