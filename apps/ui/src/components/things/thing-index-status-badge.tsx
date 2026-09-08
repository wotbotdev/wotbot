'use client';

import { Check, Circle, Clock, Loader2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';

import { type ThingIndexStatus } from './thing-detail-model';

export function ThingIndexStatusBadge({
  status,
}: {
  status: ThingIndexStatus | null | undefined;
}) {
  if (status == null) {
    return (
      <Badge variant="outline" className="gap-1.5 font-normal">
        <Loader2 className="h-3 w-3 animate-spin" />
        Checking
      </Badge>
    );
  }

  if (!status.indexed) {
    return (
      <Badge
        variant="outline"
        className="gap-1.5 font-normal text-muted-foreground"
      >
        <Circle
          className="h-2.5 w-2.5 fill-muted-foreground/30 text-muted-foreground/30"
          aria-hidden="true"
        />
        Not indexed
      </Badge>
    );
  }

  if (status.stale) {
    return (
      <Badge
        variant="secondary"
        className="gap-1.5 font-normal text-yellow-700 dark:text-yellow-400"
      >
        <Clock className="h-3 w-3" aria-hidden="true" />
        Stale index
      </Badge>
    );
  }

  return (
    <Badge variant="secondary" className="gap-1.5 font-normal text-green-700">
      <Check className="h-3 w-3" aria-hidden="true" />
      Indexed
    </Badge>
  );
}
