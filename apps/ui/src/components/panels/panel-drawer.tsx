'use client';

import { html as htmlLanguage } from '@codemirror/lang-html';
import { json as jsonLanguage } from '@codemirror/lang-json';
import {
  Code2,
  History,
  Loader2,
  Pencil,
  RotateCcw,
  Sparkles,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import {
  Group as PanelGroup,
  Panel as ResizablePanel,
  Separator as PanelResizeHandle,
} from 'react-resizable-panels';
import { toast } from 'sonner';

import { CodeEditor } from '@/components/code-editor';
import { PanelFrame } from '@/components/wotbot/chat-tool-calls/panel-frame';
import { PanelValidationDetails } from '@/components/wotbot/chat-tool-calls/panel-validation-details';
import {
  parsePanelValidation,
  type PanelValidation,
} from '@/components/wotbot/chat-tool-calls/panel-validation-model';
import { HttpError } from '@/lib/http-client';
import {
  type PanelRecord,
  type PanelVersion,
  editPanel,
  fetchPanelSource,
  fetchPanelVersions,
  restorePanelVersion,
  updatePanel,
} from '@/lib/panels-api';
import { Button } from '@/components/ui/button';
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import {
  Popover,
  PopoverContent,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { getPanelOrigin } from '@/lib/panel-origin';

type SourceTab = 'html' | 'capabilities';

const htmlExtensions = [htmlLanguage()];
const jsonExtensions = [jsonLanguage()];

function formatVersionDate(value: string | null): string {
  if (!value) {
    return 'Unknown time';
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

function versionSourceLabel(source: string): string {
  switch (source) {
    case 'initial':
      return 'Initial version';
    case 'ai':
      return 'AI edit';
    case 'restore':
      return 'Restored';
    default:
      return 'Manual edit';
  }
}

export function PanelDrawer({
  panel,
  open,
  onOpenChange,
  onChanged,
}: {
  panel: PanelRecord;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: (updated: PanelRecord) => void;
}) {
  // Bumped on every successful edit to force the live iframe to reload.
  const [version, setVersion] = useState(0);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceTab, setSourceTab] = useState<SourceTab>('html');
  const [renameOpen, setRenameOpen] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [title, setTitle] = useState(panel.title);
  const [isRenaming, setIsRenaming] = useState(false);

  // AI edit state
  const [instruction, setInstruction] = useState('');
  const [isEditing, setIsEditing] = useState(false);
  const [editFeedback, setEditFeedback] = useState<{
    validation?: PanelValidation;
    error?: string;
  } | null>(null);

  // Raw source state
  const [html, setHtml] = useState('');
  const [capsText, setCapsText] = useState('');
  const [sourceLoaded, setSourceLoaded] = useState(false);
  const [isSourceLoading, setIsSourceLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  // Version history state
  const [versions, setVersions] = useState<PanelVersion[]>([]);
  const [versionsLoaded, setVersionsLoaded] = useState(false);
  const [isVersionsLoading, setIsVersionsLoading] = useState(false);
  const [restoringVersionId, setRestoringVersionId] = useState<string | null>(
    null,
  );

  useEffect(() => {
    setTitle(panel.title);
  }, [panel.title]);

  useEffect(() => {
    if (!open) {
      setAiOpen(false);
      setInstruction('');
      setRenameOpen(false);
      setHistoryOpen(false);
      setSourceOpen(false);
      setSourceTab('html');
      setVersions([]);
      setVersionsLoaded(false);
    }
  }, [open]);

  const applied = useCallback(
    (
      updated: PanelRecord,
      options: { reloadFrame?: boolean; resetSource?: boolean } = {},
    ) => {
      onChanged(updated);
      setEditFeedback(null);
      if (options.reloadFrame ?? true) {
        setVersion((v) => v + 1);
      }
      if (options.resetSource ?? true) {
        setSourceLoaded(false);
      }
      setVersionsLoaded(false);
    },
    [onChanged],
  );

  const loadSource = useCallback(async () => {
    setIsSourceLoading(true);
    try {
      const detail = await fetchPanelSource(panel.id);
      setHtml(detail.html ?? '');
      setCapsText(JSON.stringify(detail.capabilities ?? [], null, 2));
      setSourceLoaded(true);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to load panel source',
      );
    } finally {
      setIsSourceLoading(false);
    }
  }, [panel.id]);

  const loadVersions = useCallback(async () => {
    setIsVersionsLoading(true);
    try {
      setVersions(await fetchPanelVersions(panel.id));
      setVersionsLoaded(true);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to load panel history',
      );
    } finally {
      setIsVersionsLoading(false);
    }
  }, [panel.id]);

  const handleRenameOpenChange = (nextOpen: boolean) => {
    setRenameOpen(nextOpen);
    if (nextOpen) {
      setAiOpen(false);
      setHistoryOpen(false);
      setTitle(panel.title);
    }
  };

  const handleAiOpenChange = (nextOpen: boolean) => {
    setAiOpen(nextOpen);
    if (nextOpen) {
      setRenameOpen(false);
      setHistoryOpen(false);
    } else if (!isEditing) {
      setInstruction('');
    }
  };

  const handleHistoryOpenChange = (nextOpen: boolean) => {
    setHistoryOpen(nextOpen);
    if (nextOpen) {
      setAiOpen(false);
      setRenameOpen(false);
      if (!versionsLoaded) {
        void loadVersions();
      }
    }
  };

  const handleSourceToggle = () => {
    setSourceOpen((current) => {
      const nextOpen = !current;
      if (nextOpen && !sourceLoaded) {
        void loadSource();
      }
      return nextOpen;
    });
    setAiOpen(false);
    setRenameOpen(false);
    setHistoryOpen(false);
  };

  const handleRename = async () => {
    const nextTitle = title.trim();
    if (!nextTitle) {
      toast.error('Title is required');
      setTitle(panel.title);
      return;
    }
    if (nextTitle === panel.title) {
      setRenameOpen(false);
      return;
    }
    setIsRenaming(true);
    try {
      applied(await updatePanel(panel.id, { title: nextTitle }), {
        reloadFrame: false,
        resetSource: false,
      });
      if (historyOpen) {
        void loadVersions();
      }
      setRenameOpen(false);
      toast.success('Renamed');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Rename failed');
    } finally {
      setIsRenaming(false);
    }
  };

  const handleAiEdit = async () => {
    if (!instruction.trim()) return;
    setIsEditing(true);
    setEditFeedback(null);
    try {
      const updated = await editPanel(panel.id, instruction.trim());
      applied(updated);
      setEditFeedback({
        validation: parsePanelValidation(updated.browser_validation),
      });
      setInstruction('');
      setAiOpen(false);
      setSourceOpen(false);
      if (historyOpen) {
        void loadVersions();
      }
      toast.success('Panel updated');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Edit failed';
      const detail = error instanceof HttpError ? error.detail : undefined;
      setEditFeedback({
        error: message,
        validation:
          detail && typeof detail === 'object' && 'browser_validation' in detail
            ? parsePanelValidation(detail.browser_validation)
            : undefined,
      });
      setAiOpen(false);
      toast.error(message);
    } finally {
      setIsEditing(false);
    }
  };

  const handleSaveCode = async () => {
    let capabilities;
    try {
      capabilities = JSON.parse(capsText);
    } catch {
      toast.error('Capabilities is not valid JSON');
      setSourceTab('capabilities');
      return;
    }
    setIsSaving(true);
    try {
      applied(await updatePanel(panel.id, { html, capabilities }), {
        resetSource: false,
      });
      if (historyOpen) {
        void loadVersions();
      }
      toast.success('Saved');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Save failed');
    } finally {
      setIsSaving(false);
    }
  };

  const handleRestoreVersion = async (panelVersion: PanelVersion) => {
    setRestoringVersionId(panelVersion.id);
    try {
      const updated = await restorePanelVersion(panel.id, panelVersion.id);
      applied(updated);
      if (sourceOpen) {
        await loadSource();
      }
      await loadVersions();
      toast.success('Version restored');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Restore failed');
    } finally {
      setRestoringVersionId(null);
    }
  };

  const currentVersion = versions[0]?.version;

  const frame = (
    <PanelFrame
      key={version}
      capabilities={panel.capabilities}
      className="h-full min-h-[20rem] w-full rounded-lg"
      src={`${getPanelOrigin(panel.id)}/api/panels/${encodeURIComponent(panel.id)}/render?v=${version}`}
      title={panel.title}
    />
  );

  return (
    <Drawer
      direction="right"
      handleOnly
      onOpenChange={onOpenChange}
      open={open}
    >
      <DrawerContent
        className="h-full gap-0 overflow-hidden p-0"
        style={{ width: 'min(100vw, 88rem)', maxWidth: 'none' }}
      >
        <DrawerHeader className="flex-row items-center justify-between gap-3 border-b border-border/55 px-4 py-2.5">
          <div className="min-w-0">
            <DrawerTitle className="truncate text-sm font-medium">
              {panel.title}
            </DrawerTitle>
          </div>
          <DrawerDescription className="sr-only">
            View and edit the pinned panel.
          </DrawerDescription>
          <div className="flex shrink-0 items-center gap-1.5">
            <Popover open={renameOpen} onOpenChange={handleRenameOpenChange}>
              <PopoverTrigger asChild>
                <Button
                  aria-label="Rename panel"
                  size="sm"
                  type="button"
                  variant={renameOpen ? 'secondary' : 'ghost'}
                >
                  <Pencil className="size-3.5" />
                  <span className="hidden sm:inline">Rename</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="z-[60] w-80">
                <PopoverHeader>
                  <PopoverTitle>Rename Panel</PopoverTitle>
                </PopoverHeader>
                <form
                  className="space-y-3"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void handleRename();
                  }}
                >
                  <Input
                    autoFocus
                    aria-label="Panel title"
                    disabled={isRenaming}
                    onChange={(event) => setTitle(event.target.value)}
                    value={title}
                  />
                  <div className="flex justify-end gap-2">
                    <Button
                      disabled={isRenaming}
                      onClick={() => {
                        setTitle(panel.title);
                        setRenameOpen(false);
                      }}
                      type="button"
                      variant="outline"
                    >
                      Cancel
                    </Button>
                    <Button disabled={isRenaming} type="submit">
                      {isRenaming ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : null}
                      {isRenaming ? 'Saving...' : 'Save'}
                    </Button>
                  </div>
                </form>
              </PopoverContent>
            </Popover>

            <Popover open={aiOpen} onOpenChange={handleAiOpenChange}>
              <PopoverTrigger asChild>
                <Button
                  aria-label="Ask AI to change panel"
                  size="sm"
                  type="button"
                  variant={aiOpen ? 'secondary' : 'ghost'}
                >
                  <Sparkles className="size-3.5" />
                  <span className="hidden sm:inline">Ask AI</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent
                align="end"
                className="z-[60] w-96 max-w-[calc(100vw-2rem)]"
              >
                <PopoverHeader>
                  <PopoverTitle>Ask AI to Change</PopoverTitle>
                </PopoverHeader>
                <div className="space-y-3">
                  <Textarea
                    autoFocus
                    className="min-h-32 resize-none"
                    disabled={isEditing}
                    onChange={(event) => setInstruction(event.target.value)}
                    placeholder="Add a trend chart, change the layout, or include another device."
                    value={instruction}
                  />
                  <div className="flex justify-end">
                    <Button
                      disabled={isEditing || !instruction.trim()}
                      onClick={() => void handleAiEdit()}
                    >
                      {isEditing ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : (
                        <Sparkles className="size-4" />
                      )}
                      {isEditing ? 'Updating...' : 'Apply change'}
                    </Button>
                  </div>
                </div>
              </PopoverContent>
            </Popover>

            <Popover open={historyOpen} onOpenChange={handleHistoryOpenChange}>
              <PopoverTrigger asChild>
                <Button
                  aria-label="Open panel history"
                  size="sm"
                  type="button"
                  variant={historyOpen ? 'secondary' : 'ghost'}
                >
                  <History className="size-3.5" />
                  <span className="hidden sm:inline">History</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent
                align="end"
                className="z-[60] w-96 max-w-[calc(100vw-2rem)]"
              >
                <PopoverHeader>
                  <PopoverTitle>Panel History</PopoverTitle>
                </PopoverHeader>
                <div className="max-h-[22rem] overflow-y-auto pr-1">
                  {isVersionsLoading ? (
                    <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
                      <Loader2 className="mr-2 size-4 animate-spin" />
                      Loading history
                    </div>
                  ) : versions.length > 0 ? (
                    <div className="space-y-1.5">
                      {versions.map((panelVersion) => {
                        const isCurrent =
                          panelVersion.version === currentVersion;
                        const isRestoring =
                          restoringVersionId === panelVersion.id;
                        return (
                          <div
                            className="flex items-center justify-between gap-3 rounded-md border border-border/70 px-2.5 py-2"
                            key={panelVersion.id}
                          >
                            <div className="min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-medium">
                                  Version {panelVersion.version}
                                </span>
                                {isCurrent ? (
                                  <span className="rounded-full bg-secondary px-1.5 py-0.5 text-[10px] font-medium text-secondary-foreground">
                                    Current
                                  </span>
                                ) : null}
                              </div>
                              <div className="truncate text-xs text-muted-foreground">
                                {versionSourceLabel(panelVersion.source)} -{' '}
                                {formatVersionDate(panelVersion.created_at)}
                              </div>
                              <div className="truncate text-xs text-muted-foreground/85">
                                {panelVersion.title}
                              </div>
                            </div>
                            <Button
                              aria-label={`Restore version ${panelVersion.version}`}
                              disabled={
                                isCurrent || restoringVersionId !== null
                              }
                              onClick={() =>
                                void handleRestoreVersion(panelVersion)
                              }
                              size="icon-sm"
                              type="button"
                              variant="ghost"
                            >
                              {isRestoring ? (
                                <Loader2 className="size-4 animate-spin" />
                              ) : (
                                <RotateCcw className="size-4" />
                              )}
                            </Button>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
                      No saved versions yet.
                    </div>
                  )}
                </div>
              </PopoverContent>
            </Popover>

            <Button
              aria-label="Open source editor"
              aria-pressed={sourceOpen}
              onClick={handleSourceToggle}
              size="sm"
              type="button"
              variant={sourceOpen ? 'secondary' : 'ghost'}
            >
              <Code2 className="size-3.5" />
              <span className="hidden sm:inline">Source</span>
            </Button>

            <DrawerClose asChild>
              <Button
                aria-label="Close"
                size="icon-sm"
                type="button"
                variant="ghost"
              >
                <X className="size-3.5" />
              </Button>
            </DrawerClose>
          </div>
        </DrawerHeader>

        {editFeedback ? (
          <div className="max-h-[45%] shrink-0 space-y-1 overflow-y-auto border-b border-border/55 bg-background px-4 py-3">
            <p
              className="text-sm"
              role={editFeedback.error ? 'alert' : 'status'}
            >
              {editFeedback.error ?? 'Panel updated by AI.'}
            </p>
            {editFeedback.validation ? (
              <PanelValidationDetails
                key={editFeedback.validation.reportId}
                validation={editFeedback.validation}
              />
            ) : null}
          </div>
        ) : null}

        <div className="min-h-0 flex-1 bg-muted/25">
          {sourceOpen ? (
            <PanelGroup
              className="h-full min-h-0"
              defaultLayout={{ preview: 42, source: 58 }}
              orientation="horizontal"
            >
              <ResizablePanel
                className="min-w-0"
                defaultSize="42%"
                id="preview"
                minSize="24%"
              >
                <div className="h-full p-2 sm:p-3">{frame}</div>
              </ResizablePanel>

              <PanelResizeHandle className="group relative flex w-2 shrink-0 cursor-col-resize touch-none items-center justify-center bg-border/40 transition-colors hover:bg-border focus-visible:bg-border focus-visible:outline-none">
                <div className="h-12 w-1 rounded-full bg-muted-foreground/30 transition-colors group-hover:bg-muted-foreground/55" />
              </PanelResizeHandle>

              <ResizablePanel
                className="min-w-0"
                defaultSize="58%"
                id="source"
                maxSize="76%"
                minSize="34%"
              >
                <Tabs
                  className="flex h-full min-h-0 flex-col gap-0 border-l border-border/70 bg-background"
                  onValueChange={(value) => setSourceTab(value as SourceTab)}
                  value={sourceTab}
                >
                  <div className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border/55 px-3">
                    <TabsList
                      className="h-12 max-w-full overflow-x-auto"
                      variant="line"
                    >
                      <TabsTrigger className="h-12 px-3" value="html">
                        HTML
                      </TabsTrigger>
                      <TabsTrigger className="h-12 px-3" value="capabilities">
                        Capabilities
                      </TabsTrigger>
                    </TabsList>
                    <div className="flex shrink-0 items-center gap-1.5">
                      <Button
                        disabled={isSaving || isSourceLoading || !sourceLoaded}
                        onClick={() => void handleSaveCode()}
                        type="button"
                      >
                        {isSaving ? (
                          <Loader2 className="size-4 animate-spin" />
                        ) : null}
                        {isSaving ? 'Saving...' : 'Save'}
                      </Button>
                    </div>
                  </div>

                  <TabsContent
                    className="min-h-0 flex-1 p-3"
                    forceMount
                    hidden={sourceTab !== 'html'}
                    value="html"
                  >
                    <CodeEditor
                      disabled={isSaving || isSourceLoading}
                      extensions={htmlExtensions}
                      loading={isSourceLoading}
                      onChange={setHtml}
                      value={html}
                    />
                  </TabsContent>

                  <TabsContent
                    className="min-h-0 flex-1 p-3"
                    forceMount
                    hidden={sourceTab !== 'capabilities'}
                    value="capabilities"
                  >
                    <CodeEditor
                      disabled={isSaving || isSourceLoading}
                      extensions={jsonExtensions}
                      loading={isSourceLoading}
                      onChange={setCapsText}
                      value={capsText}
                    />
                  </TabsContent>
                </Tabs>
              </ResizablePanel>
            </PanelGroup>
          ) : (
            <div className="h-full p-2 sm:p-3">{frame}</div>
          )}
        </div>
      </DrawerContent>
    </Drawer>
  );
}
