import { wotFetch } from '@/lib/wot-api';

export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const viewport =
    new URL(request.url).searchParams.get('viewport') ?? 'normal';
  if (!['normal', 'narrow'].includes(viewport))
    return new Response('Invalid viewport', { status: 400 });
  const response = await wotFetch(
    `/panel-validation/${encodeURIComponent(id)}/screenshot?viewport=${viewport}`,
    { cache: 'no-store' },
  );
  if (!response.ok)
    return new Response('Screenshot unavailable or expired', {
      status: response.status,
    });
  return new Response(response.body, {
    headers: {
      'Content-Type': 'image/png',
      'Cache-Control': 'private, no-store',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}
