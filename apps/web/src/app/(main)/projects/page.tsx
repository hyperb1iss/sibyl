import { Suspense } from 'react';

import { ProjectsSkeleton } from '@/components/suspense-boundary';
import { fetchProjects } from '@/lib/api-server';
import { ProjectsContent } from './projects-content';

export default async function ProjectsPage() {
  const projects = await fetchProjects();

  return (
    <Suspense fallback={<ProjectsSkeleton />}>
      <ProjectsContent initialProjects={projects} />
    </Suspense>
  );
}
