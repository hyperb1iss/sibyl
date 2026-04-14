import type { Metadata } from 'next';
import { Suspense } from 'react';

import { ProjectsSkeleton } from '@/components/suspense-boundary';
import { fetchProjectSummaries, fetchProjects } from '@/lib/api-server';
import { ProjectsContent } from './projects-content';

export const metadata: Metadata = {
  title: 'Projects',
  description: 'Manage your Sibyl projects',
};

export default async function ProjectsPage() {
  const [projects, projectSummaries] = await Promise.all([
    fetchProjects(),
    fetchProjectSummaries().catch(() => undefined),
  ]);

  return (
    <Suspense fallback={<ProjectsSkeleton />}>
      <ProjectsContent initialProjects={projects} initialProjectSummaries={projectSummaries} />
    </Suspense>
  );
}
