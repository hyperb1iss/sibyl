import { vi } from 'vitest';
import type { Entity } from '@/lib/api';

/**
 * Factory for creating test entities.
 */
export function createMockEntity(overrides: Partial<Entity> = {}): Entity {
  return {
    id: `entity_${Math.random().toString(36).slice(2, 14)}`,
    name: 'Test Entity',
    description: 'A test entity description',
    content: 'Test content',
    entity_type: 'pattern',
    category: 'testing',
    languages: ['typescript'],
    tags: ['test'],
    metadata: {},
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

/**
 * Factory for creating test tasks.
 */
export function createMockTask(overrides: Partial<Entity> = {}): Entity {
  return createMockEntity({
    id: `task_${Math.random().toString(36).slice(2, 14)}`,
    name: 'Test Task',
    entity_type: 'task',
    metadata: {
      status: 'todo',
      priority: 'medium',
      project_id: 'proj_test123',
      assignees: [],
      technologies: [],
      tags: [],
      ...((overrides.metadata as Record<string, unknown>) || {}),
    },
    ...overrides,
  });
}

/**
 * Factory for creating test projects.
 */
export function createMockProject(overrides: Partial<Entity> = {}): Entity {
  return createMockEntity({
    id: `proj_${Math.random().toString(36).slice(2, 14)}`,
    name: 'Test Project',
    entity_type: 'project',
    metadata: {
      repository_url: 'https://github.com/test/project',
      ...((overrides.metadata as Record<string, unknown>) || {}),
    },
    ...overrides,
  });
}

/**
 * Mock API responses for React Query.
 */
export const mockApiResponses = {
  entities: {
    list: (entities: Entity[] = []) => ({
      entities,
      total: entities.length,
      page: 1,
      page_size: 50,
    }),
  },
  tasks: {
    list: (tasks: Entity[] = []) => ({
      entities: tasks,
      total: tasks.length,
      page: 1,
      page_size: 50,
    }),
  },
  projects: {
    list: (projects: Entity[] = []) => ({
      entities: projects,
      total: projects.length,
      page: 1,
      page_size: 50,
    }),
  },
};

/**
 * Create a mock fetch function for API tests.
 */
export function createMockFetch(responses: Record<string, unknown>) {
  return vi.fn().mockImplementation((url: string) => {
    const path = new URL(url, 'http://localhost').pathname;
    const response = responses[path];

    if (response) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(response),
      });
    }

    return Promise.resolve({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ error: 'Not found' }),
    });
  });
}
