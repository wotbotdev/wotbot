'use client';

import {
  CheckCircle2,
  DatabaseZap,
  KeyRound,
  Lock,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react';
import { usePathname } from 'next/navigation';
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useRef,
  useState,
} from 'react';
import { toast } from 'sonner';

import { ConfirmDialog } from '@/components/confirm-dialog';
import { CredentialDialog } from '@/components/things/thing-detail-credential-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
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
import { httpClient } from '@/lib/http-client';
import {
  type DiscoverySource,
  type ProviderSchema,
  deleteSource,
  fetchProviderSchemas,
  fetchSources,
} from '@/lib/sources-api';

import { SourceRegistrationDialog } from './source-registration-dialog';

const PER_PAGE = 12;

function CredentialBadge({ source }: { source: DiscoverySource }) {
  if (source.credential_status === 'required') {
    return <Badge variant="destructive">Required</Badge>;
  }
  if (source.credential_status === 'configured') {
    return (
      <Badge
        variant="outline"
        className="gap-1.5 border-emerald-500/50 font-normal text-emerald-700 dark:text-emerald-400"
      >
        <CheckCircle2 className="h-3 w-3" />
        Configured
      </Badge>
    );
  }
  return (
    <Badge variant="ghost" className="font-normal text-muted-foreground">
      Not required
    </Badge>
  );
}

export function SourcesList() {
  const pathname = usePathname();
  const [search, setSearch] = useState('');
  // The `?source=` the rest of the app links with, kept only until its row has
  // been highlighted once, so later searches do not re-trigger the scroll.
  const [highlightId, setHighlightId] = useState('');
  // Read in an effect, not during render: `window.location` is still the old
  // URL while a client-side navigation renders, and `useSearchParams` would
  // opt this subtree out of hydration on a direct load.
  const [seeded, setSeeded] = useState(false);
  const deferredSearch = useDeferredValue(search);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DiscoverySource[]>([]);
  const [total, setTotal] = useState(0);
  const [pending, setPending] = useState(true);
  const [providerTitles, setProviderTitles] = useState<Record<string, string>>(
    {},
  );
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [editing, setEditing] = useState<DiscoverySource | null>(null);
  const [credentialSource, setCredentialSource] =
    useState<DiscoverySource | null>(null);
  const [removing, setRemoving] = useState<DiscoverySource | null>(null);
  const [clearingCredential, setClearingCredential] =
    useState<DiscoverySource | null>(null);
  const highlightRef = useRef<HTMLTableRowElement | null>(null);

  useEffect(() => {
    const linked =
      new URLSearchParams(window.location.search).get('source') || '';
    if (linked) {
      setSearch(linked);
      setHighlightId(linked);
    }
    setSeeded(true);
  }, []);

  useEffect(() => setPage(1), [deferredSearch]);

  // Keep the query in the URL so a filtered list can be shared and the back
  // button behaves, matching the `?source=` link the list already accepts.
  useEffect(() => {
    // Wait for the seed above, or the first pass would strip the very param it
    // is about to read.
    if (!seeded) return;
    const next = deferredSearch.trim();
    const params = new URLSearchParams(window.location.search);
    if ((params.get('source') || '') === next) return;
    if (next) params.set('source', next);
    else params.delete('source');
    const query = params.toString();
    window.history.replaceState(
      null,
      '',
      query ? `${pathname}?${query}` : pathname,
    );
  }, [deferredSearch, pathname, seeded]);

  useEffect(() => {
    void fetchProviderSchemas()
      .then((schemas: ProviderSchema[]) =>
        setProviderTitles(
          Object.fromEntries(
            schemas.map((schema) => [schema.provider, schema.title]),
          ),
        ),
      )
      .catch(() => undefined);
  }, []);

  const loadData = useCallback(async () => {
    setPending(true);
    try {
      const result = await fetchSources(page, PER_PAGE, deferredSearch);
      setData(result.data);
      setTotal(result.total);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to load sources',
      );
    } finally {
      setPending(false);
    }
  }, [deferredSearch, page]);

  useEffect(() => void loadData(), [loadData]);

  // Rows render after the fetch resolves, so the browser can never scroll to a
  // deep-linked source on its own. Do it once the row actually exists.
  useEffect(() => {
    if (!highlightId || !highlightRef.current) return;
    highlightRef.current.scrollIntoView({ block: 'center' });
    const timer = window.setTimeout(() => setHighlightId(''), 2000);
    return () => window.clearTimeout(timer);
  }, [data, highlightId]);

  async function handleDelete(source: DiscoverySource) {
    try {
      await deleteSource(source.source_id);
      toast.success(`Deleted ${source.title}`);
      await loadData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Delete failed');
    }
  }

  async function handleDeleteCredential(source: DiscoverySource) {
    try {
      await httpClient(
        `/discovery/sources/${encodeURIComponent(source.source_id)}/credentials/${encodeURIComponent(source.security_name)}`,
        { method: 'DELETE' },
      );
      toast.success(`Removed credentials for ${source.title}`);
      await loadData();
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Could not remove credentials',
      );
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const hasSearch = Boolean(deferredSearch.trim());
  const firstVisible = total === 0 ? 0 : (page - 1) * PER_PAGE + 1;
  const lastVisible = Math.min(page * PER_PAGE, total);

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Sources</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Manage persistent external discovery endpoints. Sources stay out of
            the Thing catalog until a resource is onboarded.
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => setRegistrationOpen(true)}>
            <Plus className="h-4 w-4" /> Register source
          </Button>
          <Button
            variant="outline"
            onClick={() => void loadData()}
            disabled={pending}
          >
            <RefreshCw
              className={pending ? 'h-4 w-4 animate-spin' : 'h-4 w-4'}
            />
            Refresh
          </Button>
        </div>
      </section>

      <Card className="rounded-md border-border/70 shadow-sm shadow-black/5">
        <CardContent className="space-y-4 p-4 md:p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="relative w-full max-w-xl">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                aria-label="Search sources"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search sources"
                className="pl-9"
              />
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="secondary">{total} total</Badge>
              <Badge variant="outline">
                Page {page} of {totalPages}
              </Badge>
            </div>
          </div>

          {pending ? (
            <div className="space-y-2 rounded-md border p-3">
              {['s1', 's2', 's3'].map((key) => (
                <Skeleton key={key} className="h-16 w-full rounded-md" />
              ))}
            </div>
          ) : data.length ? (
            <>
              <div className="overflow-x-auto rounded-md border">
                <Table className="min-w-[860px] table-fixed">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[34%]">Source</TableHead>
                      <TableHead className="w-[18%]">Provider</TableHead>
                      <TableHead className="w-[13%]">Network</TableHead>
                      <TableHead className="w-[16%]">Credentials</TableHead>
                      <TableHead className="w-[9%]">Things</TableHead>
                      <TableHead className="w-[10%] text-right">
                        Actions
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.map((source) => {
                      const highlighted = source.source_id === highlightId;
                      return (
                        <TableRow
                          key={source.source_id}
                          id={source.source_id}
                          ref={highlighted ? highlightRef : undefined}
                          className={
                            highlighted
                              ? 'bg-primary/5 outline outline-2 -outline-offset-2 outline-primary/40'
                              : undefined
                          }
                        >
                          <TableCell>
                            <div className="space-y-1">
                              <div className="flex items-center gap-2 font-medium">
                                <DatabaseZap className="h-4 w-4 shrink-0 text-muted-foreground" />
                                <span className="min-w-0 truncate">
                                  {source.title}
                                </span>
                              </div>
                              <p className="line-clamp-2 text-sm text-muted-foreground">
                                {source.description || source.external_id}
                              </p>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-wrap gap-1">
                              <Badge variant="outline">
                                {providerTitles[source.provider] ||
                                  source.provider}
                              </Badge>
                              {source.capabilities.includes('refresh') ? (
                                <Badge
                                  variant="secondary"
                                  className="font-normal"
                                >
                                  Refreshable
                                </Badge>
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell>
                            {source.network_access === 'private' ? (
                              <Badge
                                variant="outline"
                                className="gap-1.5 border-amber-500/50 font-normal text-amber-700 dark:text-amber-400"
                              >
                                <Lock className="h-3 w-3" />
                                Private
                              </Badge>
                            ) : (
                              <Badge
                                variant="ghost"
                                className="font-normal text-muted-foreground"
                              >
                                Public
                              </Badge>
                            )}
                          </TableCell>
                          <TableCell>
                            <CredentialBadge source={source} />
                          </TableCell>
                          <TableCell className="tabular-nums">
                            {source.dependent_thing_count}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end">
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    aria-label={`Actions for ${source.title}`}
                                  >
                                    <MoreHorizontal className="h-4 w-4" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent
                                  align="end"
                                  className="w-52"
                                >
                                  <DropdownMenuItem
                                    onSelect={() => setEditing(source)}
                                  >
                                    <Pencil className="h-3.5 w-3.5" /> Edit
                                  </DropdownMenuItem>
                                  {source.security_scheme !== 'nosec' ? (
                                    <DropdownMenuItem
                                      onSelect={() =>
                                        setCredentialSource(source)
                                      }
                                    >
                                      <KeyRound className="h-3.5 w-3.5" />
                                      {source.credential_status === 'configured'
                                        ? 'Replace credentials'
                                        : 'Add credentials'}
                                    </DropdownMenuItem>
                                  ) : null}
                                  {source.credential_status === 'configured' ? (
                                    <DropdownMenuItem
                                      variant="destructive"
                                      onSelect={() =>
                                        setClearingCredential(source)
                                      }
                                    >
                                      <Trash2 className="h-3.5 w-3.5" /> Clear
                                      credentials
                                    </DropdownMenuItem>
                                  ) : null}
                                  <DropdownMenuSeparator />
                                  <DropdownMenuItem
                                    variant="destructive"
                                    onSelect={() => setRemoving(source)}
                                  >
                                    <Trash2 className="h-3.5 w-3.5" /> Remove
                                    source
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>

              <div className="flex flex-col gap-3 rounded-md border bg-card px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-muted-foreground">
                  Showing {firstVisible}-{lastVisible} of {total} sources
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page <= 1}
                    onClick={() => setPage((value) => Math.max(1, value - 1))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page >= totalPages}
                    onClick={() =>
                      setPage((value) => Math.min(totalPages, value + 1))
                    }
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <div className="rounded-md border border-dashed px-6 py-12 text-center">
              <h2 className="text-xl font-semibold tracking-tight">
                No sources found
              </h2>
              <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
                {hasSearch
                  ? `No sources match "${deferredSearch.trim()}".`
                  : 'Register a catalog, ToolHive registry, or dataspace endpoint.'}
              </p>
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {hasSearch ? (
                  <Button variant="outline" onClick={() => setSearch('')}>
                    Clear search
                  </Button>
                ) : null}
                <Button onClick={() => setRegistrationOpen(true)}>
                  <Plus className="h-4 w-4" /> Register source
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <SourceRegistrationDialog
        open={registrationOpen || editing !== null}
        onOpenChange={(next) => {
          setRegistrationOpen(next);
          if (!next) setEditing(null);
        }}
        source={editing}
        onRegistered={() => void loadData()}
      />

      {credentialSource ? (
        <CredentialDialog
          open
          onOpenChange={(next) => {
            if (!next) setCredentialSource(null);
          }}
          sourceId={credentialSource.source_id}
          secDef={{
            name: credentialSource.security_name,
            scheme: credentialSource.security_scheme,
          }}
          onSaved={() => {
            setCredentialSource(null);
            void loadData();
          }}
        />
      ) : null}

      <ConfirmDialog
        destructive
        open={clearingCredential !== null}
        onOpenChange={(next) => {
          if (!next) setClearingCredential(null);
        }}
        confirmLabel="Clear credentials"
        title={`Clear credentials for "${clearingCredential?.title ?? ''}"?`}
        description="The stored secret is deleted and cannot be recovered. Discovery through this source will fail until new credentials are added."
        onConfirm={async () => {
          if (clearingCredential) {
            await handleDeleteCredential(clearingCredential);
          }
        }}
      />

      <ConfirmDialog
        destructive
        open={removing !== null}
        onOpenChange={(next) => {
          if (!next) setRemoving(null);
        }}
        confirmLabel="Remove"
        title={`Remove "${removing?.title ?? ''}"?`}
        description="This removes the source and its stored credentials. Sources with dependent Things cannot be removed."
        onConfirm={async () => {
          if (removing) await handleDelete(removing);
        }}
      />
    </div>
  );
}
