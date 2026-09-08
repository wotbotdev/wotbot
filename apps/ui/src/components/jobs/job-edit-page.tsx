'use client';

import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { useRouter } from 'next/navigation';
import { Save } from 'lucide-react';
import { toast } from 'sonner';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { JobActionFields } from '@/components/jobs/form/job-action-fields';
import { JobEnabledField } from '@/components/jobs/form/job-enabled-field';
import { JobFormCard } from '@/components/jobs/form/job-form-card';
import { FormPageHeader } from '@/components/form-page-header';
import {
  canEditTimeSchedule,
  type EditJobFormState,
  type JobScheduleKind,
  toEditJobFormState,
  toUpdateJobPayload,
  validateEditJobForm,
} from '@/components/jobs/form/job-form-model';
import { JobScheduleFields } from '@/components/jobs/form/job-schedule-fields';
import { getScheduleLabel, getStatusLabel } from '@/lib/job-formatters';
import { type JobRecord, fetchJob, updateJob } from '@/lib/jobs-api';
import { getLocalReturnTo } from '@/lib/return-to';
import { useUnsavedChangesGuard } from '@/hooks/use-unsaved-changes-guard';

interface JobEditPageProps {
  jobId: string;
  returnTo?: string;
}

function getScheduleDescription(scheduleKind: JobScheduleKind): string {
  if (scheduleKind === 'interval') {
    return 'How often this job runs. Saving resets the next run.';
  }
  if (scheduleKind === 'cron') {
    return 'The cron expression and timezone for this recurring job.';
  }
  return 'When this one-time job runs.';
}

function getScheduleBadgeLabel(
  job: JobRecord,
  scheduleKind: JobScheduleKind | null,
): string {
  if (!scheduleKind) return getScheduleLabel(job);
  if (scheduleKind === 'interval') return 'Interval';
  if (scheduleKind === 'cron') return 'Cron';
  return 'Once';
}

export function JobEditPage({ jobId, returnTo }: JobEditPageProps) {
  const router = useRouter();
  const [job, setJob] = useState<JobRecord | null>(null);
  const [form, setForm] = useState<EditJobFormState | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const record = await fetchJob(jobId);
      setJob(record);
      setForm(toEditJobFormState(record));
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : 'Failed to load job',
      );
    } finally {
      setIsLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  const setField = useCallback(
    <K extends keyof EditJobFormState>(
      field: K,
      value: EditJobFormState[K],
    ) => {
      setForm((current) =>
        current ? { ...current, [field]: value } : current,
      );
    },
    [],
  );

  const scheduleKind =
    job && form && canEditTimeSchedule(job) ? form.scheduleKind : null;
  const cancelHref = getLocalReturnTo(returnTo, `/jobs/${jobId}`);
  const isDirty =
    job && form
      ? JSON.stringify(form) !== JSON.stringify(toEditJobFormState(job))
      : false;

  useUnsavedChangesGuard(
    Boolean(isDirty && !isSubmitting),
    'You have unsaved job changes. Leave without saving?',
  );

  const validationError = useMemo(() => {
    if (!job || !form) return null;
    return validateEditJobForm(job, form);
  }, [form, job]);

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!job || !form || validationError) return;

      setIsSubmitting(true);
      try {
        const updatedJob = await updateJob(
          job.id,
          toUpdateJobPayload(job, form),
        );
        toast.success('Job updated.');
        router.push(getLocalReturnTo(returnTo, `/jobs/${updatedJob.id}`));
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : 'Failed to update job',
        );
        setIsSubmitting(false);
      }
    },
    [form, job, returnTo, router, validationError],
  );

  if (isLoading) {
    return (
      <Card className="rounded-md border-border/70">
        <CardContent className="flex min-h-56 items-center justify-center">
          <Spinner className="size-6 text-primary" />
        </CardContent>
      </Card>
    );
  }

  if (loadError || !job || !form) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Unable to load job</AlertTitle>
        <AlertDescription>{loadError ?? 'Job not found.'}</AlertDescription>
      </Alert>
    );
  }

  const actionCard = (
    <JobFormCard
      title={job.action.kind === 'analysis' ? 'Analysis code' : 'Prompt'}
      description="What the job runs each time it is triggered."
    >
      <JobActionFields
        actionKind={job.action.kind}
        prompt={form.prompt}
        analysisCode={form.analysisCode}
        onPromptChange={(value) => setField('prompt', value)}
        onAnalysisCodeChange={(value) => setField('analysisCode', value)}
        showLabels={false}
      />
    </JobFormCard>
  );

  const settingsDescription = scheduleKind
    ? 'Name, enabled state, and schedule in one place.'
    : 'Name and enabled state. Event bindings are fixed after creation.';

  return (
    <form className="space-y-5" onSubmit={(event) => void handleSubmit(event)}>
      <FormPageHeader
        title="Edit job"
        description="Update the name, enabled state, schedule, and action content. The trigger source and event binding are fixed after creation."
        cancelHref={cancelHref}
        submitLabel="Save"
        submitIcon={<Save className="h-4 w-4" />}
        isSubmitting={isSubmitting}
        disabled={isSubmitting || Boolean(validationError)}
      />

      <JobFormCard
        title="Settings"
        description={settingsDescription}
        contentClassName={
          scheduleKind
            ? 'grid gap-5 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]'
            : 'space-y-4'
        }
        headerAction={
          <div className="flex flex-wrap gap-2">
            <Badge variant="outline">{getStatusLabel(job.action.kind)}</Badge>
            <Badge variant="outline">
              {getScheduleBadgeLabel(job, scheduleKind)}
            </Badge>
          </div>
        }
      >
        <section className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <div className="space-y-2">
            <label className="text-sm font-medium">Name</label>
            <Input
              value={form.name}
              onChange={(event) => setField('name', event.target.value)}
              placeholder="Daily operations summary"
            />
          </div>
          <JobEnabledField
            compact
            enabled={form.enabled}
            onEnabledChange={(value) => setField('enabled', value)}
          />
        </section>

        {scheduleKind ? (
          <section className="space-y-2">
            <JobScheduleFields
              scheduleKind={scheduleKind}
              intervalSeconds={form.intervalSeconds}
              runAt={form.runAt}
              cronExpression={form.cronExpression}
              cronTimezone={form.cronTimezone}
              onIntervalSecondsChange={(value) =>
                setField('intervalSeconds', value)
              }
              onRunAtChange={(value) => setField('runAt', value)}
              onCronExpressionChange={(value) =>
                setField('cronExpression', value)
              }
              onCronTimezoneChange={(value) => setField('cronTimezone', value)}
              onScheduleKindChange={(value) => setField('scheduleKind', value)}
            />
            <p className="text-xs text-muted-foreground">
              {getScheduleDescription(scheduleKind)}
            </p>
          </section>
        ) : null}
      </JobFormCard>

      {actionCard}

      {validationError ? (
        <p className="text-sm text-destructive">{validationError}</p>
      ) : null}
    </form>
  );
}
