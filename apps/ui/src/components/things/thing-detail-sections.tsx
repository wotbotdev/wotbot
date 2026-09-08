'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { DatabaseZap, ExternalLink, Pencil, Trash2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { type ThingRecord } from '@/lib/things-api';
import { type RuntimeAffordanceType } from '@/lib/wot-runtime-api';
import { isDiscoveredOrigin } from '@/lib/virtual-things';
import { type VirtualThingBinding } from '@/lib/virtual-things-api';
import { withReturnTo } from '@/lib/return-to';

import {
  type ActionDef,
  type EventDef,
  type PropertyDef,
  type SecurityDefinition,
  type StoredCredential,
  type ThingIndexStatus,
} from './thing-detail-model';
import { ThingSemanticSection } from './thing-detail-summary';
import {
  ThingActionsSection,
  ThingEventsSection,
  ThingPropertiesSection,
  ThingSecuritySection,
} from './thing-detail-tables';
import { ThingIndexStatusBadge } from './thing-index-status-badge';
import { VirtualThingStatusToggle } from './virtual-thing-status-toggle';
import { ThingRefreshDialog } from './thing-refresh-dialog';

const DETAIL_TABS_TRIGGER_CLASSNAME =
  'flex-none rounded-none border-b-2 border-transparent px-4 py-2.5 font-medium text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none data-active:border-primary data-active:bg-transparent data-active:text-foreground data-active:shadow-none';

export interface ThingDetailLayoutProps {
  thing: ThingRecord;
  title: string;
  description: string;
  securityStr: string;
  properties: PropertyDef[];
  actions: ActionDef[];
  events: EventDef[];
  securityDefs: SecurityDefinition[];
  credentialMap: Map<string, StoredCredential>;
  indexStatus: ThingIndexStatus | null;
  semanticSummary?: string;
  semanticIndexed: boolean;
  isDeleting: boolean;
  onDelete: () => Promise<void> | void;
  onDeleteCredential: (securityName: string) => Promise<void> | void;
  onOpenCredential: (definition: SecurityDefinition) => void;
  refreshSupported?: boolean;
  onRefreshed?: () => Promise<void> | void;
  isVirtual?: boolean;
  /** When set (drawer context), shows an "Open" button linking to the page. */
  openHref?: string;
  bindings?: Map<string, VirtualThingBinding>;
  onRun?: (affordanceType: RuntimeAffordanceType, name: string) => void;
  runRequiresBinding?: boolean;
  onOpenBinding?: (key: string) => void;
  bindingHref?: (key: string) => string;
  activeBindingKey?: string | null;
  defaultTab?: 'properties' | 'events' | 'actions';
}

export function ThingDetailPageLayout({
  thing,
  title,
  description,
  securityStr,
  properties,
  actions,
  events,
  securityDefs,
  credentialMap,
  indexStatus,
  semanticSummary,
  semanticIndexed,
  isDeleting,
  onDelete,
  onDeleteCredential,
  onOpenCredential,
  refreshSupported = false,
  onRefreshed,
  isVirtual = false,
  openHref,
  bindings,
  onRun,
  runRequiresBinding,
  onOpenBinding,
  bindingHref,
  activeBindingKey,
  defaultTab,
}: ThingDetailLayoutProps) {
  const pathname = usePathname();
  const editHref = withReturnTo(
    `/things/${encodeURIComponent(thing.id)}/edit`,
    pathname,
  );

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-2">
          <div className="space-y-1">
            <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
            <p className="break-all font-mono text-xs text-muted-foreground">
              {thing.id}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ThingIndexStatusBadge status={indexStatus} />
            {isVirtual ? <Badge variant="secondary">Virtual</Badge> : null}
            {isDiscoveredOrigin(thing.origin) ? (
              <Badge
                variant="outline"
                className="border-blue-500/50 text-blue-600 dark:text-blue-400"
              >
                Resource · {thing.origin.provider || 'Discovered'}
              </Badge>
            ) : null}
            <Badge variant="outline">
              {properties.length} propert{properties.length === 1 ? 'y' : 'ies'}
            </Badge>
            <Badge variant="outline">
              {actions.length} action{actions.length === 1 ? '' : 's'}
            </Badge>
            <Badge variant="outline">
              {events.length} event{events.length === 1 ? '' : 's'}
            </Badge>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {openHref ? (
            <Button asChild variant="outline">
              <Link href={openHref}>
                <ExternalLink className="h-4 w-4" />
                Open
              </Link>
            </Button>
          ) : null}
          {isVirtual ? <VirtualThingStatusToggle thingId={thing.id} /> : null}
          {isDiscoveredOrigin(thing.origin) && thing.origin.source_id ? (
            <Button asChild variant="outline">
              <Link
                href={`/sources?source=${encodeURIComponent(thing.origin.source_id)}`}
              >
                <DatabaseZap className="h-4 w-4" />
                View source
              </Link>
            </Button>
          ) : null}
          {refreshSupported && onRefreshed ? (
            <ThingRefreshDialog thingId={thing.id} onRefreshed={onRefreshed} />
          ) : null}
          {!isVirtual ? (
            <Button asChild variant="outline">
              <Link href={editHref}>
                <Pencil className="h-4 w-4" />
                Edit JSON
              </Link>
            </Button>
          ) : null}
          <ConfirmDialog
            destructive
            confirmLabel={isDeleting ? 'Removing...' : 'Remove'}
            description={
              isVirtual
                ? 'This permanently removes the Virtual Thing definition, bindings, and produced Thing. This cannot be undone.'
                : 'This permanently removes the Thing Description and related credentials. This cannot be undone.'
            }
            onConfirm={onDelete}
            title={`Remove "${thing.title}"?`}
            trigger={
              <Button variant="destructive" disabled={isDeleting}>
                {isDeleting ? (
                  <Spinner className="size-4" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
                {isDeleting ? 'Removing...' : 'Remove Thing'}
              </Button>
            }
          />
        </div>
      </section>

      <section className="space-y-4">
        <Card className="rounded-md border-border/70 shadow-sm shadow-black/5">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-base">Description</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="whitespace-pre-wrap break-words text-sm leading-6 text-muted-foreground">
              {description || 'No description provided.'}
            </p>
          </CardContent>
        </Card>
      </section>

      <ThingSecuritySection
        securityStr={securityStr}
        securityDefs={securityDefs}
        credentialMap={credentialMap}
        onDeleteCredential={onDeleteCredential}
        onOpenCredential={onOpenCredential}
      />

      <Tabs defaultValue={defaultTab ?? 'properties'} className="space-y-5">
        <div className="overflow-x-auto">
          <TabsList
            variant="line"
            className="h-auto min-w-max gap-0 rounded-none border-b border-border/80 bg-transparent p-0"
          >
            <TabsTrigger
              value="properties"
              className={DETAIL_TABS_TRIGGER_CLASSNAME}
            >
              Properties ({properties.length})
            </TabsTrigger>
            <TabsTrigger
              value="events"
              className={DETAIL_TABS_TRIGGER_CLASSNAME}
            >
              Events ({events.length})
            </TabsTrigger>
            <TabsTrigger
              value="actions"
              className={DETAIL_TABS_TRIGGER_CLASSNAME}
            >
              Actions ({actions.length})
            </TabsTrigger>
            <TabsTrigger
              value="semantic"
              className={DETAIL_TABS_TRIGGER_CLASSNAME}
            >
              Semantic Summary
            </TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="properties" className="mt-0">
          <ThingPropertiesSection
            properties={properties}
            bindings={bindings}
            onRun={onRun}
            runRequiresBinding={runRequiresBinding}
            onOpenBinding={onOpenBinding}
            bindingHref={bindingHref}
            activeKey={activeBindingKey}
          />
        </TabsContent>

        <TabsContent value="events" className="mt-0">
          <ThingEventsSection
            events={events}
            bindings={bindings}
            onRun={onRun}
            runRequiresBinding={runRequiresBinding}
            onOpenBinding={onOpenBinding}
            bindingHref={bindingHref}
            activeKey={activeBindingKey}
          />
        </TabsContent>

        <TabsContent value="actions" className="mt-0">
          <ThingActionsSection
            actions={actions}
            bindings={bindings}
            onRun={onRun}
            runRequiresBinding={runRequiresBinding}
            onOpenBinding={onOpenBinding}
            bindingHref={bindingHref}
            activeKey={activeBindingKey}
          />
        </TabsContent>

        <TabsContent value="semantic" className="mt-0">
          <ThingSemanticSection
            indexStatus={indexStatus}
            semanticSummary={semanticSummary}
            semanticIndexed={semanticIndexed}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
