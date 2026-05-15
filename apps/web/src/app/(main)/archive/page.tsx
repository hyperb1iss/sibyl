import { redirect } from 'next/navigation';

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function ArchivePage({ searchParams }: PageProps) {
  const params = await searchParams;
  const nextParams = new URLSearchParams();

  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) {
      for (const item of value) {
        nextParams.append(key, item);
      }
      continue;
    }
    if (value) {
      nextParams.set(key, value);
    }
  }

  const query = nextParams.toString();
  redirect(query ? `/memory/captures?${query}` : '/memory/captures');
}
