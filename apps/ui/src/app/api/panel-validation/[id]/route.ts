import { wotFetch } from '@/lib/wot-api';

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const response = await wotFetch(
    `/panel-validation/${encodeURIComponent(id)}`,
    { cache: 'no-store' },
  );
  return new Response(await response.text(), {
    status: response.status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'private, no-store',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}
