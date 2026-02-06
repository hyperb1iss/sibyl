import { redirect } from 'next/navigation';

interface AgentDetailPageProps {
  params: Promise<{ id: string }>;
}

export default async function AgentDetailPage({ params }: AgentDetailPageProps) {
  const { id } = await params;
  redirect(`/agents?id=${id}`);
}
