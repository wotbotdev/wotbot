'use client';

import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Maximize2, RefreshCw, Search, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { useRouter, useSearchParams } from 'next/navigation';

import { PanelFrame } from '@/components/wotbot/chat-tool-calls/panel-frame';
import { PanelDrawer } from '@/components/panels/panel-drawer';
import { type PanelRecord, deletePanel, fetchPanels } from '@/lib/panels-api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { getPanelOrigin } from '@/lib/panel-origin';

const PANEL_PREVIEW_VIEWPORT = {
  width: 1024,
  height: 768,
} as const;

function getPanelSearchableText(panel: PanelRecord): string {
  return [
    panel.title,
    panel.id,
    panel.source_thread_id,
    ...panel.capabilities.flatMap((capability) => [
      capability.thingId,
      ...capability.affordances,
      ...capability.ops,
    ]),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

/** Renders children only once scrolled into view, to avoid mounting every
 *  panel's live bridge at once. */
function WhenVisible({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || visible) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: '200px' },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visible]);

  return (
    <div ref={ref} className="h-full w-full">
      {visible ? children : null}
    </div>
  );
}

function PanelPreview({ panel }: { panel: PanelRecord }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(0.36);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const updateScale = () => {
      const rect = container.getBoundingClientRect();
      const nextScale = Math.min(
        rect.width / PANEL_PREVIEW_VIEWPORT.width,
        rect.height / PANEL_PREVIEW_VIEWPORT.height,
        1,
      );

      if (Number.isFinite(nextScale) && nextScale > 0) {
        setScale(nextScale);
      }
    };

    updateScale();
    const observer = new ResizeObserver(updateScale);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden bg-muted/30"
    >
      <div
        className="flex-none overflow-hidden rounded-md border border-border/55 bg-background shadow-sm"
        style={{
          height: PANEL_PREVIEW_VIEWPORT.height,
          transform: `scale(${scale})`,
          transformOrigin: 'center',
          width: PANEL_PREVIEW_VIEWPORT.width,
        }}
      >
        <WhenVisible>
          <PanelFrame
            capabilities={panel.capabilities}
            className="h-full w-full rounded-none border-0"
            interactive={false}
            src={`${getPanelOrigin(panel.id)}/api/panels/${encodeURIComponent(panel.id)}/render`}
            title={panel.title}
          />
        </WhenVisible>
      </div>
    </div>
  );
}

function PanelCard({
  panel,
  onOpen,
  onDeleted,
}: {
  panel: PanelRecord;
  onOpen: (id: string) => void;
  onDeleted: (id: string) => void;
}) {
  const [isDeleting, setIsDeleting] = useState(false);

  const handleDelete = async () => {
    setIsDeleting(true);
    try {
      await deletePanel(panel.id);
      toast.success('Panel deleted');
      onDeleted(panel.id);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to delete panel',
      );
      setIsDeleting(false);
    }
  };

  const deviceCount = new Set(panel.capabilities.map((c) => c.thingId)).size;

  return (
    <Card className="overflow-hidden py-0">
      <CardHeader className="gap-0 border-b border-border/55 px-3 py-2.5">
        <CardTitle className="truncate text-sm">{panel.title}</CardTitle>
        <CardAction>
          <Button
            aria-label="Delete panel"
            disabled={isDeleting}
            onClick={() => void handleDelete()}
            size="icon-xs"
            type="button"
            variant="ghost"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </CardAction>
      </CardHeader>

      <CardContent className="p-0">
        {/* Inert preview: scripts and bridge calls stay disabled until opened. */}
        <button
          aria-label={`Open ${panel.title}`}
          className="group relative block h-[18rem] w-full cursor-pointer"
          onClick={() => onOpen(panel.id)}
          type="button"
        >
          <div className="pointer-events-none absolute inset-0">
            <PanelPreview panel={panel} />
          </div>
          <div className="absolute inset-0 flex items-center justify-center bg-background/0 opacity-0 transition group-hover:bg-background/40 group-hover:opacity-100">
            <span className="flex items-center gap-1.5 rounded-md bg-background/90 px-2.5 py-1 text-xs font-medium shadow-sm">
              <Maximize2 className="size-3.5" />
              Open
            </span>
          </div>
        </button>
      </CardContent>

      <CardFooter className="justify-between px-3 py-2 text-[0.7rem] text-muted-foreground">
        <span>
          {deviceCount} device{deviceCount === 1 ? '' : 's'}
        </span>
        {panel.created_at ? (
          <span>{new Date(panel.created_at).toLocaleDateString()}</span>
        ) : null}
      </CardFooter>
    </Card>
  );
}

export function PanelsList() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get('panelId');
  const selectPanel = (id: string | null) => {
    const query = new URLSearchParams(searchParams.toString());
    if (id) query.set('panelId', id);
    else query.delete('panelId');
    router.replace(`/panels${query.size ? `?${query}` : ''}`, {
      scroll: false,
    });
  };
  const [search, setSearch] = useState('');
  const deferredSearch = useDeferredValue(search);
  const [panels, setPanels] = useState<PanelRecord[]>([]);
  const [isPending, setIsPending] = useState(true);
  const [hasLoaded, setHasLoaded] = useState(false);

  const loadData = useCallback(async () => {
    setIsPending(true);
    try {
      setPanels(await fetchPanels());
      setHasLoaded(true);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to load panels',
      );
    } finally {
      setIsPending(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleChanged = useCallback((updated: PanelRecord) => {
    setPanels((prev) =>
      prev.map((p) => (p.id === updated.id ? { ...p, ...updated } : p)),
    );
  }, []);

  const handleDeleted = useCallback((id: string) => {
    setPanels((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const visiblePanels = useMemo(() => {
    const query = deferredSearch.trim().toLowerCase();
    if (!query) return panels;
    return panels.filter((panel) =>
      getPanelSearchableText(panel).includes(query),
    );
  }, [deferredSearch, panels]);

  const hasSearch = deferredSearch.trim().length > 0;
  const selectedPanel = panels.find((panel) => panel.id === selectedId);

  return (
    <div className="space-y-4">
      <section className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Panels</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Pinned interactive panels. Pin a panel from a chat to keep it here —
            it stays even if you delete the conversation. Open one to interact,
            edit it with the assistant, or tweak its code.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => void loadData()}
            disabled={isPending}
          >
            <RefreshCw
              className={isPending ? 'h-4 w-4 animate-spin' : 'h-4 w-4'}
            />
            Refresh
          </Button>
        </div>
      </section>

      <div className="flex flex-col gap-2 rounded-md border border-border/70 bg-card/70 px-2.5 py-2 shadow-sm shadow-black/5 sm:flex-row sm:items-center sm:justify-between">
        <div className="relative w-full sm:max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search panels"
            className="h-8 pl-8"
          />
        </div>
        <div className="flex min-h-7 items-center gap-1.5 text-xs text-muted-foreground">
          <Badge variant="secondary" className="h-6 px-2 text-[11px]">
            {visiblePanels.length} visible
          </Badge>
          <Badge variant="outline" className="h-6 px-2 text-[11px]">
            {panels.length} pinned
          </Badge>
        </div>
      </div>

      {selectedId && hasLoaded && !isPending && !selectedPanel ? (
        <div role="alert" className="rounded-md border px-4 py-3 text-sm">
          This panel no longer exists or is unavailable.
          <Button variant="link" onClick={() => selectPanel(null)}>
            Dismiss
          </Button>
        </div>
      ) : null}
      {selectedPanel ? (
        <PanelDrawer
          key={selectedPanel.id}
          panel={selectedPanel}
          open
          onChanged={handleChanged}
          onOpenChange={(open) => {
            if (!open) selectPanel(null);
          }}
        />
      ) : null}

      {isPending ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {['s1', 's2', 's3'].map((key) => (
            <Skeleton key={key} className="h-[24rem] w-full rounded-xl" />
          ))}
        </div>
      ) : visiblePanels.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {visiblePanels.map((panel) => (
            <PanelCard
              key={panel.id}
              panel={panel}
              onOpen={selectPanel}
              onDeleted={handleDeleted}
            />
          ))}
        </div>
      ) : (
        <div className="rounded-md border border-dashed px-6 py-12 text-center">
          <h2 className="text-xl font-semibold tracking-tight">
            {hasSearch ? 'No panels found' : 'No pinned panels'}
          </h2>
          <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
            {hasSearch
              ? `No panels match "${deferredSearch.trim()}".`
              : 'Ask the assistant to build a control panel or dashboard, then click the pin icon to keep it here.'}
          </p>
          {hasSearch ? (
            <div className="mt-5 flex justify-center">
              <Button variant="outline" onClick={() => setSearch('')}>
                Clear search
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
