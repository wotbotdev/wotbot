'use client';

import { Suspense } from 'react';

import { PanelsList } from '@/components/panels/panels-list';
import { AppShell } from '@/components/app-shell';

export default function PanelsPage() {
  return (
    <AppShell>
      <Suspense>
        <PanelsList />
      </Suspense>
    </AppShell>
  );
}
