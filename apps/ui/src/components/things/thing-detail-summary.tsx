'use client';

import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';

import {
  formatDateTime,
  formatIndexerLabel,
  type ThingIndexStatus,
} from './thing-detail-model';
import { ThingIndexStatusBadge } from './thing-index-status-badge';

function SemanticBadgeList({
  label,
  items,
  emptyText,
}: {
  label: string;
  items?: string[];
  emptyText: string;
}) {
  const values = (items ?? []).filter((item) => item.trim().length > 0);

  return (
    <div className="space-y-2">
      <p className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      {values.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {values.map((item) => (
            <Badge
              key={`${label}:${item}`}
              variant="outline"
              className="font-normal"
            >
              {item}
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      )}
    </div>
  );
}

export function ThingSemanticSection({
  indexStatus,
  semanticSummary,
  semanticIndexed,
}: {
  indexStatus: ThingIndexStatus | null;
  semanticSummary?: string;
  semanticIndexed: boolean;
}) {
  return (
    <Card className="rounded-md border-border/70 shadow-sm shadow-black/5">
      <CardHeader>
        <CardTitle className="text-base">Semantic summary</CardTitle>
        <CardDescription>
          Latest summary and extracted terms from the semantic indexer.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <ThingIndexStatusBadge status={indexStatus} />
          {semanticIndexed && indexStatus?.summary_source ? (
            <Badge variant="outline" className="font-normal">
              Source {formatIndexerLabel(indexStatus.summary_source)}
            </Badge>
          ) : null}
          {semanticIndexed && indexStatus?.summary_model ? (
            <Badge variant="outline" className="font-normal">
              Model {indexStatus.summary_model}
            </Badge>
          ) : null}
          {semanticIndexed && indexStatus?.td_hash_match === false ? (
            <Badge
              variant="outline"
              className="border-yellow-500/60 font-normal text-yellow-700 dark:text-yellow-400"
            >
              Stale snapshot
            </Badge>
          ) : null}
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <p className="text-xs uppercase tracking-wider text-muted-foreground">
              Indexed at
            </p>
            <p className="text-sm text-muted-foreground">
              {formatDateTime(indexStatus?.indexed_at)}
            </p>
          </div>

          {indexStatus === null ? (
            <div className="rounded-lg border px-4 py-3 text-sm text-muted-foreground">
              Checking semantic index metadata...
            </div>
          ) : !semanticIndexed ? (
            <div className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
              This thing has not been indexed yet, so no semantic summary is
              available.
            </div>
          ) : semanticSummary ? (
            <div className="rounded-lg border bg-muted/20 px-4 py-3">
              <p className="whitespace-pre-line text-sm leading-6">
                {semanticSummary}
              </p>
            </div>
          ) : (
            <div className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
              The thing is indexed, but the stored semantic summary is empty.
            </div>
          )}

          {semanticIndexed && indexStatus?.stale ? (
            <p className="text-sm text-yellow-700 dark:text-yellow-400">
              The semantic index snapshot is older than the current Thing
              Description and may be out of date.
            </p>
          ) : null}
        </div>

        <Separator />

        <div className="grid gap-6 xl:grid-cols-2">
          <SemanticBadgeList
            label="Indexed properties"
            items={indexStatus?.property_names}
            emptyText="No properties indexed."
          />
          <SemanticBadgeList
            label="Indexed actions"
            items={indexStatus?.action_names}
            emptyText="No actions indexed."
          />
          <SemanticBadgeList
            label="Indexed events"
            items={indexStatus?.event_names}
            emptyText="No events indexed."
          />
        </div>
      </CardContent>
    </Card>
  );
}
