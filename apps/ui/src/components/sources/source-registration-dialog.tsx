'use client';

import { AlertTriangle, Loader2 } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';

import { CredentialDialog } from '@/components/things/thing-detail-credential-dialog';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import {
  type DiscoverySource,
  type ProviderSchema,
  type SourceCredentialChallenge,
  type SourceDraft,
  type SourceOnboardingResult,
  fetchProviderSchemas,
  registerDetectedSource,
  saveSource,
} from '@/lib/sources-api';

const ACRONYMS = new Set(['api', 'id', 'url', 'uri', 'edc', 'dcat', 'mcp']);

/** Turns a provider config key such as `base_url` into `Base URL`. */
function humanizeField(field: string): string {
  return field
    .split('_')
    .map((word) =>
      ACRONYMS.has(word.toLowerCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(' ');
}

/**
 * The config field a detected URL belongs in, so switching between the two
 * registration modes carries the URL across instead of dropping it.
 */
function primaryUrlField(provider: ProviderSchema | null): string | null {
  const required = provider?.config_schema.required || [];
  const properties = provider?.config_schema.properties || {};
  return (
    required.find((field) => properties[field]?.format === 'uri') ||
    required[0] ||
    null
  );
}

export function SourceRegistrationDialog({
  open,
  onOpenChange,
  onRegistered,
  source,
  initialDraft,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRegistered: (
    source: DiscoverySource,
    onboarding?: SourceOnboardingResult,
  ) => void;
  source?: DiscoverySource | null;
  initialDraft?: SourceDraft | null;
}) {
  const editing = Boolean(source);
  const [providers, setProviders] = useState<ProviderSchema[]>([]);
  const [providersPending, setProvidersPending] = useState(true);
  const [mode, setMode] = useState<'detect' | 'manual'>('detect');
  const [url, setUrl] = useState('');
  const [providerName, setProviderName] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [networkAccess, setNetworkAccess] = useState<'public' | 'private'>(
    'public',
  );
  const [privateConfirmed, setPrivateConfirmed] = useState(false);
  const [securityName, setSecurityName] = useState('source_sc');
  const [securityScheme, setSecurityScheme] = useState('nosec');
  const [config, setConfig] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<{
    message: string;
    details?: string[];
  } | null>(null);
  const [credentialChallenge, setCredentialChallenge] =
    useState<SourceCredentialChallenge | null>(null);
  const [pendingSource, setPendingSource] = useState<DiscoverySource | null>(
    null,
  );
  const credentialSaved = useRef(false);

  // The draft arrives as a fresh object on every stream update, so the reset
  // below keys off its content. Without this a tick mid-edit wipes the form.
  const draftKey = JSON.stringify(initialDraft ?? null);
  const draftRef = useRef(initialDraft);
  draftRef.current = initialDraft;

  useEffect(() => {
    if (!open) return;
    setProvidersPending(true);
    void fetchProviderSchemas()
      .then(setProviders)
      .catch((error) =>
        setFormError({
          message:
            error instanceof Error ? error.message : 'Could not load providers',
        }),
      )
      .finally(() => setProvidersPending(false));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setPrivateConfirmed(false);
    setFormError(null);
    if (source) {
      setMode('manual');
      setProviderName(source.provider);
      setTitle(source.title);
      setDescription(source.description);
      setTags(source.tags.join(', '));
      setNetworkAccess(source.network_access);
      setSecurityName(source.security_name);
      setSecurityScheme(source.security_scheme);
      setConfig(
        Object.fromEntries(
          Object.entries(source.config).map(([key, value]) => [
            key,
            String(value),
          ]),
        ),
      );
      return;
    }
    const draft = draftRef.current || {};
    // Detection is the recommended path, so it is where an empty dialog lands.
    // Only a draft that already names a provider skips ahead to the manual tab.
    setMode(draft.provider ? 'manual' : 'detect');
    setUrl(draft.url || '');
    setProviderName(draft.provider || '');
    setTitle(draft.title || '');
    setDescription(draft.description || '');
    setTags((draft.tags || []).join(', '));
    setNetworkAccess('public');
    setSecurityName('source_sc');
    setSecurityScheme(draft.security_scheme || 'nosec');
    setConfig(
      Object.fromEntries(
        Object.entries({
          ...(draft.provider && draft.url ? { url: draft.url } : {}),
          ...(draft.config || {}),
        }).map(([key, value]) => [key, String(value ?? '')]),
      ),
    );
  }, [draftKey, open, source]);

  // Never silently substitute a different provider: an unresolved name is an
  // error the user has to see, not one to paper over with `providers[0]`.
  const provider = useMemo(
    () => providers.find((item) => item.provider === providerName) ?? null,
    [providerName, providers],
  );
  const unknownProvider = Boolean(
    providerName && !providersPending && !provider,
  );

  useEffect(() => {
    if (editing || providersPending || !providers.length) return;
    if (!providerName) setProviderName(providers[0].provider);
  }, [editing, providerName, providers, providersPending]);

  useEffect(() => {
    if (!provider || editing) return;
    if (!draftRef.current?.security_scheme) {
      setSecurityScheme(provider.default_security_scheme);
    }
    setConfig((current) => ({
      ...Object.fromEntries(
        Object.entries(provider.config_schema.properties || {}).map(
          ([field, schema]) => [field, String(schema.default ?? '')],
        ),
      ),
      ...current,
    }));
  }, [editing, provider]);

  /** Carries a typed URL between the detect and configure modes. */
  function handleModeChange(next: 'detect' | 'manual') {
    const field = primaryUrlField(provider);
    if (next === 'manual' && field && url.trim() && !config[field]?.trim()) {
      setConfig((current) => ({ ...current, [field]: url.trim() }));
    }
    if (next === 'detect' && field && !url.trim() && config[field]?.trim()) {
      setUrl(config[field].trim());
    }
    setFormError(null);
    setMode(next);
  }

  async function handleSubmit() {
    setSaving(true);
    setFormError(null);
    try {
      const result =
        !editing && mode === 'detect'
          ? await registerDetectedSource(url, networkAccess)
          : await saveSource(
              {
                provider: provider?.provider || '',
                title,
                description,
                tags: tags
                  .split(',')
                  .map((item) => item.trim())
                  .filter(Boolean),
                network_access: networkAccess,
                security: { name: securityName, scheme: securityScheme },
                config: Object.fromEntries(
                  Object.entries(config).map(([field, value]) => [
                    field,
                    provider?.config_schema.properties?.[field]?.type ===
                    'number'
                      ? Number(value)
                      : value,
                  ]),
                ),
              },
              source?.source_id,
            );
      if (result.unsupported_source || !result.source) {
        // The probe evidence is one line per attempt; keep it that way rather
        // than joining it into a paragraph nobody can scan.
        setFormError({
          message: 'This URL could not be registered as a discovery source.',
          details: result.probe_evidence,
        });
        return;
      }
      const onboarding = result.onboarding;
      if (result.credential_challenge) {
        // Registration is not finished yet, so say nothing: the credential
        // prompt is the next step, and the resubmit behind it reports the
        // real outcome. Leave this dialog mounted so that resubmit has
        // somewhere to show its progress.
        credentialSaved.current = false;
        setPendingSource(result.source);
        setCredentialChallenge(result.credential_challenge);
        return;
      }
      // One toast for one outcome: a source that saved but could not onboard
      // its Thing is a warning, not a success followed by a warning.
      if (onboarding?.thing) {
        toast.success(
          `Source saved. ${onboarding.thing.title} is ready in Things.`,
        );
      } else if (onboarding?.message) {
        toast.warning(`Source saved. ${onboarding.message}`);
      } else {
        toast.success(editing ? 'Source updated' : 'Source registered');
      }
      onOpenChange(false);
      onRegistered(result.source, onboarding);
    } catch (error) {
      setFormError({
        message:
          error instanceof Error ? error.message : 'Could not save source',
      });
    } finally {
      setSaving(false);
    }
  }

  const configFields = Object.entries(provider?.config_schema.properties || {});
  const requiredFields = provider?.config_schema.required || [];
  const requiresPrivateConfirmation =
    networkAccess === 'private' && !privateConfirmed;
  const networkFields = (
    <NetworkFields
      networkAccess={networkAccess}
      privateConfirmed={privateConfirmed}
      setNetworkAccess={setNetworkAccess}
      setPrivateConfirmed={setPrivateConfirmed}
    />
  );

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {editing ? 'Edit discovery source' : 'Register discovery source'}
            </DialogTitle>
            <DialogDescription>
              Sources let you discover data and services. Registering an OpenAPI
              source with one service also adds its Thing automatically.
            </DialogDescription>
          </DialogHeader>

          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
            {editing ? (
              <ManualFields
                config={config}
                configFields={configFields}
                description={description}
                editing
                networkFields={networkFields}
                provider={provider}
                providers={providers}
                providersPending={providersPending}
                requiredFields={requiredFields}
                securityName={securityName}
                securityScheme={securityScheme}
                setConfig={setConfig}
                setDescription={setDescription}
                setProviderName={setProviderName}
                setSecurityName={setSecurityName}
                setSecurityScheme={setSecurityScheme}
                setTags={setTags}
                setTitle={setTitle}
                tags={tags}
                title={title}
                unknownProvider={unknownProvider}
                unknownProviderName={providerName}
              />
            ) : (
              <Tabs
                value={mode}
                onValueChange={(next) =>
                  handleModeChange(next as 'detect' | 'manual')
                }
              >
                <TabsList>
                  <TabsTrigger value="detect">Detect URL</TabsTrigger>
                  <TabsTrigger value="manual">Configure provider</TabsTrigger>
                </TabsList>
                <TabsContent value="detect" className="space-y-4">
                  <p className="text-sm text-muted-foreground">
                    Paste an endpoint and we probe it to work out which provider
                    handles it.
                  </p>
                  <Field label="Source URL" required>
                    <Input
                      type="url"
                      required
                      value={url}
                      onChange={(event) => setUrl(event.target.value)}
                      placeholder="https://data.example/"
                    />
                  </Field>
                  {networkFields}
                </TabsContent>
                <TabsContent value="manual" className="space-y-4">
                  <p className="text-sm text-muted-foreground">
                    Pick the provider yourself when you already know how the
                    endpoint should be read.
                  </p>
                  <ManualFields
                    config={config}
                    configFields={configFields}
                    description={description}
                    editing={false}
                    networkFields={networkFields}
                    provider={provider}
                    providers={providers}
                    providersPending={providersPending}
                    requiredFields={requiredFields}
                    securityName={securityName}
                    securityScheme={securityScheme}
                    setConfig={setConfig}
                    setDescription={setDescription}
                    setProviderName={setProviderName}
                    setSecurityName={setSecurityName}
                    setSecurityScheme={setSecurityScheme}
                    setTags={setTags}
                    setTitle={setTitle}
                    tags={tags}
                    title={title}
                    unknownProvider={unknownProvider}
                    unknownProviderName={providerName}
                  />
                </TabsContent>
              </Tabs>
            )}

            {formError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-foreground"
              >
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
                <div className="space-y-1.5 text-foreground">
                  <p>{formError.message}</p>
                  {formError.details?.length ? (
                    <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                      {/* Probe evidence repeats lines verbatim, so index keys
                          are the only stable identity here. */}
                      {formError.details.map((line, index) => (
                        <li key={`${index}-${line}`}>{line}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              </div>
            ) : null}

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  saving ||
                  requiresPrivateConfirmation ||
                  unknownProvider ||
                  (mode === 'manual' && (providersPending || !provider))
                }
              >
                {saving ? <Loader2 className="animate-spin" /> : null}
                {editing ? 'Save source' : 'Confirm registration'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {credentialChallenge ? (
        <CredentialDialog
          open
          onOpenChange={(nextOpen) => {
            if (!nextOpen) {
              setCredentialChallenge(null);
              if (pendingSource && !credentialSaved.current) {
                onOpenChange(false);
                onRegistered(pendingSource);
              }
              credentialSaved.current = false;
              setPendingSource(null);
            }
          }}
          sourceId={credentialChallenge.source_id}
          secDef={{
            name: credentialChallenge.security_name,
            scheme: credentialChallenge.scheme,
          }}
          onSaved={() => {
            credentialSaved.current = true;
            void handleSubmit();
          }}
        />
      ) : null}
    </>
  );
}

function FieldLabel({
  label,
  required,
}: {
  label: string;
  required?: boolean;
}) {
  return (
    <span>
      {label}
      {required ? (
        <>
          <span aria-hidden className="ml-0.5 text-destructive">
            *
          </span>
          <span className="sr-only"> (required)</span>
        </>
      ) : null}
    </span>
  );
}

function FieldHint({ hint }: { hint?: string }) {
  if (!hint) return null;
  return (
    <span className="block text-xs font-normal text-muted-foreground">
      {hint}
    </span>
  );
}

/** Wrapper for a native control, which the surrounding label already names. */
function Field({
  children,
  hint,
  label,
  required,
}: {
  children: React.ReactNode;
  hint?: string;
  label: string;
  required?: boolean;
}) {
  return (
    <label className="block space-y-1.5 text-sm font-medium">
      <FieldLabel label={label} required={required} />
      {children}
      <FieldHint hint={hint} />
    </label>
  );
}

/**
 * Wrapper for a `Select`, whose trigger is a button and so is not labelled by
 * a wrapping `<label>`. It gets an explicit `aria-labelledby` instead.
 */
function SelectField({
  children,
  disabled,
  hint,
  label,
  onValueChange,
  placeholder,
  required,
  value,
}: {
  children: React.ReactNode;
  disabled?: boolean;
  hint?: string;
  label: string;
  onValueChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  value: string;
}) {
  const labelId = useId();
  return (
    <div className="space-y-1.5 text-sm font-medium">
      <span className="block" id={labelId}>
        <FieldLabel label={label} required={required} />
      </span>
      <Select value={value} disabled={disabled} onValueChange={onValueChange}>
        <SelectTrigger aria-labelledby={labelId} className="h-9 w-full">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>{children}</SelectContent>
      </Select>
      <FieldHint hint={hint} />
    </div>
  );
}

function ManualFields({
  config,
  configFields,
  description,
  editing,
  networkFields,
  provider,
  providers,
  providersPending,
  requiredFields,
  securityName,
  securityScheme,
  setConfig,
  setDescription,
  setProviderName,
  setSecurityName,
  setSecurityScheme,
  setTags,
  setTitle,
  tags,
  title,
  unknownProvider,
  unknownProviderName,
}: {
  config: Record<string, string>;
  configFields: [
    string,
    { default?: string | number; format?: string; type?: string },
  ][];
  description: string;
  editing: boolean;
  networkFields: React.ReactNode;
  provider: ProviderSchema | null;
  providers: ProviderSchema[];
  providersPending: boolean;
  requiredFields: string[];
  securityName: string;
  securityScheme: string;
  setConfig: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  setDescription: (value: string) => void;
  setProviderName: (value: string) => void;
  setSecurityName: (value: string) => void;
  setSecurityScheme: (value: string) => void;
  setTags: (value: string) => void;
  setTitle: (value: string) => void;
  tags: string;
  title: string;
  unknownProvider: boolean;
  unknownProviderName: string;
}) {
  return (
    <div className="space-y-4">
      {providersPending ? (
        <Field label="Provider" required>
          <Skeleton className="h-9 w-full rounded-lg" />
        </Field>
      ) : (
        <SelectField
          label="Provider"
          required
          disabled={editing || !providers.length}
          placeholder="Select a provider"
          value={provider?.provider || ''}
          onValueChange={(value) => {
            setProviderName(value);
            setConfig({});
          }}
        >
          {providers.map((item) => (
            <SelectItem key={item.provider} value={item.provider}>
              {item.title}
            </SelectItem>
          ))}
        </SelectField>
      )}
      {unknownProvider ? (
        <p
          role="alert"
          className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200"
        >
          Provider <code>{unknownProviderName}</code> is not registered on this
          server.{' '}
          {editing
            ? 'This source cannot be saved as-is — remove it and register the endpoint again.'
            : 'Pick one of the available providers instead.'}
        </p>
      ) : null}
      <Field label="Title">
        <Input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
      </Field>
      <Field label="Description">
        <Textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </Field>
      <Field label="Tags" hint="Comma separated.">
        <Input value={tags} onChange={(event) => setTags(event.target.value)} />
      </Field>
      {networkFields}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Security name">
          <Input
            value={securityName}
            onChange={(event) => setSecurityName(event.target.value)}
          />
        </Field>
        <SelectField
          label="Credential scheme"
          value={securityScheme}
          onValueChange={setSecurityScheme}
        >
          {(provider?.security_schemes || ['nosec']).map((scheme) => (
            <SelectItem key={scheme} value={scheme}>
              {scheme}
            </SelectItem>
          ))}
        </SelectField>
      </div>
      {providersPending && !configFields.length
        ? ['c1', 'c2'].map((key) => (
            <Skeleton key={key} className="h-14 w-full rounded-lg" />
          ))
        : configFields.map(([field, schema]) => (
            <Field
              key={field}
              label={humanizeField(field)}
              required={requiredFields.includes(field)}
            >
              <Input
                type={
                  schema.format === 'uri'
                    ? 'url'
                    : schema.type === 'number'
                      ? 'number'
                      : 'text'
                }
                required={requiredFields.includes(field)}
                step={schema.type === 'number' ? 'any' : undefined}
                value={config[field] || ''}
                onChange={(event) =>
                  setConfig((current) => ({
                    ...current,
                    [field]: event.target.value,
                  }))
                }
              />
            </Field>
          ))}
    </div>
  );
}

function NetworkFields({
  networkAccess,
  privateConfirmed,
  setNetworkAccess,
  setPrivateConfirmed,
}: {
  networkAccess: 'public' | 'private';
  privateConfirmed: boolean;
  setNetworkAccess: (value: 'public' | 'private') => void;
  setPrivateConfirmed: (value: boolean) => void;
}) {
  return (
    <div className="space-y-2">
      <SelectField
        label="Network access"
        value={networkAccess}
        onValueChange={(value) => {
          setNetworkAccess(value as 'public' | 'private');
          setPrivateConfirmed(false);
        }}
      >
        <SelectItem value="public">Public network only</SelectItem>
        <SelectItem value="private">Private network allowed</SelectItem>
      </SelectField>
      {networkAccess === 'private' ? (
        <label className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200">
          <input
            className="mt-1"
            type="checkbox"
            checked={privateConfirmed}
            onChange={(event) => setPrivateConfirmed(event.target.checked)}
          />
          I confirm that this source may be probed and contacted on the private
          network.
        </label>
      ) : null}
    </div>
  );
}
