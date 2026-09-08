'use client';

import {
  useCallback,
  useDeferredValue,
  useEffect,
  useRef,
  useState,
} from 'react';
import Link from 'next/link';
import { Eye, Plus, RefreshCw, Search, Upload } from 'lucide-react';
import { toast } from 'sonner';

import { type ThingRecord, fetchThings } from '@/lib/things-api';
import { isDiscoveredOrigin, isVirtualThingId } from '@/lib/virtual-things';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { type ThingIndexStatus } from '@/components/things/thing-detail-model';
import { ThingDetailDrawer } from '@/components/things/thing-detail-drawer';
import { ThingFileUpload } from '@/components/things/thing-file-upload';
import { ThingIndexStatusBadge } from '@/components/things/thing-index-status-badge';

const PER_PAGE = 12;

export function ThingsList() {
  const [search, setSearch] = useState('');
  const deferredSearch = useDeferredValue(search);
  const [originFilter, setOriginFilter] = useState<string>('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<ThingRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [isPending, setIsPending] = useState(true);
  const [indexStatuses, setIndexStatuses] = useState<
    Record<string, ThingIndexStatus>
  >({});
  const [selectedThingId, setSelectedThingId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const requestedIndexStatusIds = useRef<Set<string>>(new Set());
  const isMounted = useRef(true);

  useEffect(
    () => () => {
      isMounted.current = false;
    },
    [],
  );

  useEffect(() => {
    setPage(1);
  }, [deferredSearch, originFilter]);

  const loadData = useCallback(async () => {
    setIsPending(true);
    try {
      const result = await fetchThings(
        page,
        PER_PAGE,
        deferredSearch,
        originFilter,
      );
      setData(result.data);
      setTotal(result.total);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to load things',
      );
    } finally {
      setIsPending(false);
    }
  }, [page, deferredSearch, originFilter]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  // Lazy-load index statuses for visible things
  useEffect(() => {
    const missingRecords = data.filter(
      (record) =>
        indexStatuses[record.id] === undefined &&
        !requestedIndexStatusIds.current.has(record.id),
    );
    if (missingRecords.length === 0) return;

    for (const record of missingRecords) {
      requestedIndexStatusIds.current.add(record.id);
      void fetch(`/api/index-status/${encodeURIComponent(record.id)}`)
        .then((res) => {
          if (!res.ok) {
            requestedIndexStatusIds.current.delete(record.id);
            return null;
          }
          return res.json();
        })
        .then((status) => {
          if (!status) {
            requestedIndexStatusIds.current.delete(record.id);
            return;
          }
          if (!isMounted.current) return;
          setIndexStatuses((prev) => ({
            ...prev,
            [record.id]: status as ThingIndexStatus,
          }));
        })
        .catch(() => {
          requestedIndexStatusIds.current.delete(record.id);
        });
    }
  }, [data, indexStatuses]);

  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const hasSearch = deferredSearch.trim().length > 0;
  const firstVisible = data.length ? (page - 1) * PER_PAGE + 1 : 0;
  const lastVisible = data.length ? (page - 1) * PER_PAGE + data.length : 0;

  const handleDeleted = useCallback((thingId: string) => {
    requestedIndexStatusIds.current.delete(thingId);
    setSelectedThingId(null);
    setData((current) => current.filter((record) => record.id !== thingId));
    setTotal((current) => Math.max(0, current - 1));
    setIndexStatuses((current) => {
      const next = { ...current };
      delete next[thingId];
      return next;
    });
  }, []);

  const handleUploadComplete = useCallback(
    (result: { allSucceeded: boolean }) => {
      void loadData();
      if (result.allSucceeded) {
        setUploadOpen(false);
      }
    },
    [loadData],
  );

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Things</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Browse and manage Thing Descriptions. View any thing to inspect or
            edit the full document.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => setUploadOpen(true)}
            type="button"
          >
            <Upload className="h-4 w-4" />
            Upload
          </Button>
          <Button asChild>
            <Link href="/things/create">
              <Plus className="h-4 w-4" />
              Create
            </Link>
          </Button>
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

      <Card className="rounded-md border-border/70 shadow-sm shadow-black/5">
        <CardContent className="space-y-4 p-4 md:p-5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="relative w-full lg:max-w-xl">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search things"
                className="pl-9"
              />
            </div>
            <div className="flex min-h-8 items-center gap-2 text-sm text-muted-foreground">
              <Badge
                variant={originFilter === '' ? 'default' : 'outline'}
                className="cursor-pointer"
                onClick={() => {
                  setOriginFilter('');
                }}
              >
                All
              </Badge>
              <Badge
                variant={originFilter === 'discovery' ? 'default' : 'outline'}
                className="cursor-pointer"
                onClick={() => {
                  setOriginFilter(
                    originFilter === 'discovery' ? '' : 'discovery',
                  );
                }}
              >
                Discovered
              </Badge>
              <Badge variant="secondary">{total} total</Badge>
              <Badge variant="outline">
                Page {page} of {totalPages}
              </Badge>
            </div>
          </div>

          {isPending ? (
            <div className="space-y-2 rounded-md border p-3">
              {['r1', 'r2', 'r3', 'r4', 'r5'].map((key) => (
                <Skeleton key={key} className="h-12 w-full rounded-md" />
              ))}
            </div>
          ) : data.length > 0 ? (
            <>
              <div className="overflow-x-auto rounded-md border">
                <Table className="min-w-[980px] table-fixed">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[42%]">Thing</TableHead>
                      <TableHead className="w-[34%]">Identifier</TableHead>
                      <TableHead className="w-[16%]">Index status</TableHead>
                      <TableHead className="w-[8%] text-right">View</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.map((record) => (
                      <TableRow key={record.id}>
                        <TableCell className="min-w-[280px]">
                          <div className="space-y-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <button
                                className="font-medium transition-colors hover:text-primary"
                                onClick={() => setSelectedThingId(record.id)}
                                type="button"
                              >
                                {record.title}
                              </button>
                              {isVirtualThingId(record.id) ? (
                                <Badge variant="secondary">Virtual</Badge>
                              ) : null}
                              {isDiscoveredOrigin(record.origin) ? (
                                <Badge
                                  variant="outline"
                                  className="border-blue-500/50 text-blue-600 dark:text-blue-400"
                                >
                                  Resource ·{' '}
                                  {record.origin.provider || 'Discovered'}
                                </Badge>
                              ) : null}
                            </div>
                            <p className="line-clamp-2 max-w-xl text-sm leading-5 text-muted-foreground">
                              {record.description || 'No description provided.'}
                            </p>
                          </div>
                        </TableCell>
                        <TableCell className="max-w-[320px] font-mono text-xs text-muted-foreground">
                          <span className="block truncate">{record.id}</span>
                        </TableCell>
                        <TableCell>
                          <ThingIndexStatusBadge
                            status={indexStatuses[record.id]}
                          />
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center justify-end">
                            <Button
                              onClick={() => setSelectedThingId(record.id)}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              <Eye className="h-3.5 w-3.5" />
                              View
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div className="flex flex-col gap-3 rounded-md border bg-card px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-muted-foreground">
                  Showing {firstVisible}-{lastVisible} of {total} things
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page <= 1}
                    onClick={() => setPage((c) => Math.max(1, c - 1))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page >= totalPages}
                    onClick={() => setPage((c) => Math.min(totalPages, c + 1))}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <div className="rounded-md border border-dashed px-6 py-12 text-center">
              <h2 className="text-xl font-semibold tracking-tight">
                No things found
              </h2>
              <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
                {hasSearch
                  ? `No things match "${deferredSearch.trim()}".`
                  : 'Create the first Thing Description to get started.'}
              </p>
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {hasSearch && (
                  <Button variant="outline" onClick={() => setSearch('')}>
                    Clear search
                  </Button>
                )}
                <Button
                  variant="outline"
                  onClick={() => setUploadOpen(true)}
                  type="button"
                >
                  <Upload className="h-4 w-4" />
                  Upload
                </Button>
                <Button asChild>
                  <Link href="/things/create">
                    <Plus className="h-4 w-4" />
                    Create
                  </Link>
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <ThingDetailDrawer
        onDeleted={handleDeleted}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) {
            setSelectedThingId(null);
          }
        }}
        open={selectedThingId !== null}
        thingId={selectedThingId}
      />

      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Upload Thing Descriptions</DialogTitle>
            <DialogDescription>
              Add one or more JSON files to create Thing records.
            </DialogDescription>
          </DialogHeader>
          <ThingFileUpload onUploadComplete={handleUploadComplete} />
        </DialogContent>
      </Dialog>
    </div>
  );
}
