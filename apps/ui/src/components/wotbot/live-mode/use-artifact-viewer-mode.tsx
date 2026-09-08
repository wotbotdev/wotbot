import { File, ImageIcon, LineChart, PanelsTopLeft } from 'lucide-react';
import { useCallback, useMemo, useState, type ReactNode } from 'react';

import type { LiveModeArtifact } from '@/components/wotbot/assistant/artifacts';
import { artifactKey } from '@/components/wotbot/chat-tool-call-model';

const artifactLabels = {
  image: { noun: 'image', Icon: ImageIcon },
  plotly: { noun: 'chart', Icon: LineChart },
  web: { noun: 'panel', Icon: PanelsTopLeft },
  file: { noun: 'file', Icon: File },
};

type ReopenChipInfo = { icon: ReactNode; label: string };

type ArtifactViewerMode = {
  inViewer: boolean;
  dismissViewer: () => void;
  reopenViewer: () => void;
  showReopenChip: boolean;
  reopenChipInfo: ReopenChipInfo | null;
};

export function useArtifactViewerMode({
  artifacts,
  latestAssistantText,
  latestUserTranscript,
}: {
  artifacts: LiveModeArtifact[];
  latestAssistantText: string | null;
  latestUserTranscript: string | null;
}): ArtifactViewerMode {
  const artifactSignature = useMemo(
    () => artifacts.map(artifactKey).join('|'),
    [artifacts],
  );
  const hasArtifact = !!artifactSignature && !!latestAssistantText;
  const [trackedSignature, setTrackedSignature] = useState<string | null>(null);
  const [transcriptAtArrival, setTranscriptAtArrival] = useState<string | null>(
    null,
  );
  const [manuallyDismissedSignature, setManuallyDismissedSignature] = useState<
    string | null
  >(null);
  const nextTrackedSignature = hasArtifact ? artifactSignature : null;
  if (nextTrackedSignature !== trackedSignature) {
    setTrackedSignature(nextTrackedSignature);
    setTranscriptAtArrival(nextTrackedSignature ? latestUserTranscript : null);
  }

  const transcriptAdvanced =
    hasArtifact && latestUserTranscript !== transcriptAtArrival;
  const inViewer =
    hasArtifact &&
    !transcriptAdvanced &&
    artifactSignature !== manuallyDismissedSignature;

  const dismissViewer = useCallback(() => {
    setManuallyDismissedSignature(artifactSignature);
  }, [artifactSignature]);

  const reopenViewer = useCallback(() => {
    setManuallyDismissedSignature(null);
    setTranscriptAtArrival(latestUserTranscript);
  }, [latestUserTranscript]);

  const showReopenChip = hasArtifact && !inViewer;
  const reopenChipInfo = useMemo<ReopenChipInfo | null>(() => {
    if (artifacts.length === 0) return null;
    const kind = artifacts[0].kind;
    const { noun, Icon } = artifacts.every((artifact) => artifact.kind === kind)
      ? artifactLabels[kind]
      : { noun: 'result', Icon: LineChart };
    const label = artifacts.length > 1 ? `${artifacts.length} ${noun}s` : noun;
    return {
      icon: <Icon className="size-4" />,
      label: `View ${label}`,
    };
  }, [artifacts]);

  return {
    inViewer,
    dismissViewer,
    reopenViewer,
    showReopenChip,
    reopenChipInfo,
  };
}
