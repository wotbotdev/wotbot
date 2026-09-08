import { ArtifactPreview } from '@/components/wotbot/chat-tool-call-cards';
import { artifactKey } from '@/components/wotbot/chat-tool-call-model';
import type { LiveModeArtifact } from '@/components/wotbot/assistant/artifacts';
import { WebInterfaceArtifactView } from '@/components/wotbot/chat-tool-calls/web-interface-card';

function LiveModeArtifactPreview({
  artifact,
  fill = false,
}: {
  artifact: LiveModeArtifact;
  fill?: boolean;
}) {
  if (artifact.kind === 'web') {
    return <WebInterfaceArtifactView artifact={artifact} fill={fill} />;
  }
  return <ArtifactPreview artifact={artifact} fill={fill} />;
}

export function LiveModeArtifactViewer({
  artifacts,
}: {
  artifacts: LiveModeArtifact[];
}) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-4 pb-28 pt-14 md:px-8">
      {artifacts.length === 1 ? (
        <LiveModeArtifactPreview artifact={artifacts[0]} fill />
      ) : (
        <div className="grid h-full w-full max-w-6xl grid-cols-1 gap-3 sm:grid-cols-2">
          {artifacts.map((artifact) => (
            <div
              className="min-h-0 overflow-hidden rounded-xl border border-border bg-background/70 p-2 shadow-sm"
              key={artifactKey(artifact)}
            >
              <LiveModeArtifactPreview artifact={artifact} fill />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
